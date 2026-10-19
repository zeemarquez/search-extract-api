"""RSS / Atom / JSON Feed reading and feed discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import feedparser
from lxml import html as lxml_html

from ..config import Settings
from ..core.cache import Cache, make_key
from ..core.dates import ensure_utc, parse_datetime
from ..core.http import HttpResult, HttpService
from ..core.lang import detect_language, normalize_language
from ..core.merge import merge_results
from ..core.security import Principal
from ..core.text import collapse_ws, strip_html, truncate
from ..core.urls import canonicalize_url, host_of, is_http_url, site_of
from ..errors import ApiError, ErrorCode
from ..schemas.common import ErrorInfo, Result, SourceStatus
from ..schemas.feeds import (
    DiscoveredFeed,
    DiscoverResponse,
    FeedInfo,
    FeedResponse,
    FeedsRequest,
    FeedsResponse,
)

if TYPE_CHECKING:
    from ..sessions.models import SessionHandle
    from ..sessions.service import SessionService

logger = logging.getLogger(__name__)

FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/feed+json, "
    "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
)
FEED_TYPES = {
    "application/rss+xml": "rss",
    "application/atom+xml": "atom",
    "application/feed+json": "jsonfeed",
    "application/rdf+xml": "rdf",
}
COMMON_FEED_PATHS = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/feeds/posts/default",
    "/?feed=rss2",
)
_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def _first_image(entry: Any, base: str) -> str | None:
    for key in ("media_thumbnail", "media_content"):
        for media in entry.get(key) or []:
            url = media.get("url")
            medium = (media.get("medium") or media.get("type") or "image").lower()
            if url and ("image" in medium or key == "media_thumbnail"):
                return urljoin(base, url)
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            return urljoin(base, link.get("href"))
    image = entry.get("image")
    if isinstance(image, dict) and image.get("href"):
        return urljoin(base, image["href"])
    for field in ("content", "summary"):
        value = entry.get(field)
        if isinstance(value, list):
            value = " ".join(v.get("value", "") for v in value if isinstance(v, dict))
        if isinstance(value, str):
            match = _IMG_SRC.search(value)
            if match:
                return urljoin(base, match.group(1))
    return None


def parse_feed_bytes(
    content: bytes, url: str, provider: str | None = None
) -> tuple[FeedInfo, list[Result]]:
    """Parses RSS/Atom (via feedparser) or JSON Feed into the common schema."""
    provider = provider or f"feed:{site_of(url)}"
    stripped = content.lstrip()
    if stripped.startswith(b"{"):
        return _parse_json_feed(stripped, url, provider)
    parsed = feedparser.parse(content)
    if not parsed.get("version") and not parsed.entries:
        raise ApiError(
            ErrorCode.FEED_PARSE_ERROR,
            "The URL is not a valid RSS/Atom feed (try /feeds/discover to find one).",
            details={"url": url},
        )
    meta = parsed.feed
    feed_language = normalize_language(meta.get("language"))
    link = meta.get("link") or url
    info = FeedInfo(
        url=url,
        title=strip_html(meta.get("title")) or None,
        link=link,
        description=truncate(strip_html(meta.get("subtitle") or meta.get("description")), 500)
        or None,
        language=feed_language,
        updated_at=parse_datetime(meta.get("updated_parsed") or meta.get("published_parsed")),
        format=parsed.get("version") or None,
    )
    results: list[Result] = []
    for entry in parsed.entries:
        entry_link = entry.get("link") or entry.get("id")
        if not is_http_url(entry_link):
            continue
        entry_link = urljoin(link, entry_link)
        title = strip_html(entry.get("title")) or None
        summary_html = entry.get("summary") or ""
        if not summary_html and entry.get("content"):
            summary_html = entry["content"][0].get("value", "")
        snippet = truncate(strip_html(summary_html), 500) or None
        source_href = (entry.get("source") or {}).get("href") if entry.get("source") else None
        language = feed_language or detect_language(" ".join(filter(None, [title, snippet])))
        extra: dict[str, Any] = {"feed_url": url, "feed_title": info.title}
        if entry.get("author"):
            extra["author"] = collapse_ws(entry.get("author"))
        tags = [t.get("term") for t in entry.get("tags") or [] if t.get("term")]
        if tags:
            extra["tags"] = tags[:20]
        if entry.get("id") and entry.get("id") != entry_link:
            extra["guid"] = entry.get("id")
        results.append(
            Result(
                url=entry_link,
                title=title,
                source=site_of(source_href) if source_href else site_of(entry_link),
                published_at=parse_datetime(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                ),
                language=language,
                snippet=snippet,
                image=_first_image(entry, entry_link),
                providers=[provider],
                extra=extra,
            )
        )
    info.item_count = len(results)
    return info, results


def _parse_json_feed(content: bytes, url: str, provider: str) -> tuple[FeedInfo, list[Result]]:
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ApiError(ErrorCode.FEED_PARSE_ERROR, "Invalid JSON feed.") from exc
    if not isinstance(data, dict) or "jsonfeed.org" not in str(data.get("version", "")):
        raise ApiError(ErrorCode.FEED_PARSE_ERROR, "The URL is not a valid feed.")
    language = normalize_language(data.get("language"))
    info = FeedInfo(
        url=url,
        title=data.get("title"),
        link=data.get("home_page_url"),
        description=truncate(data.get("description"), 500),
        language=language,
        format="jsonfeed",
    )
    results: list[Result] = []
    for item in data.get("items") or []:
        link = item.get("url") or item.get("external_url")
        if not is_http_url(link):
            continue
        snippet = (
            item.get("summary") or strip_html(item.get("content_html")) or item.get("content_text")
        )
        authors = item.get("authors") or ([item["author"]] if item.get("author") else [])
        extra: dict[str, Any] = {"feed_url": url, "feed_title": info.title}
        names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
        if names:
            extra["author"] = ", ".join(names)
        results.append(
            Result(
                url=link,
                title=item.get("title"),
                source=site_of(link),
                published_at=parse_datetime(
                    item.get("date_published") or item.get("date_modified")
                ),
                language=normalize_language(item.get("language")) or language,
                snippet=truncate(collapse_ws(snippet), 500) or None,
                image=item.get("image") or item.get("banner_image"),
                providers=[provider],
                extra=extra,
            )
        )
    info.item_count = len(results)
    return info, results


class FeedService:
    def __init__(
        self,
        *,
        settings: Settings,
        http: HttpService,
        cache: Cache,
        sessions: SessionService | None = None,
    ) -> None:
        self.settings = settings
        self.http = http
        self.cache = cache
        self.sessions = sessions

    async def _fetch(self, url: str, session: SessionHandle | None = None) -> HttpResult:
        if session is not None:
            await self.http.politeness.wait_session(session.id)
        result = await self.http.fetch(
            url, headers={"Accept": FEED_ACCEPT}, cookies=session.state if session else None
        )
        if result.status_code == 404 or result.status_code == 410:
            raise ApiError(ErrorCode.FEED_NOT_FOUND, f"Feed not found (HTTP {result.status_code}).")
        if result.status_code >= 400:
            raise ApiError(
                ErrorCode.UPSTREAM_HTTP_ERROR,
                f"The feed returned HTTP {result.status_code}.",
                details={"status_code": result.status_code},
            )
        return result

    async def read(
        self,
        url: str,
        *,
        principal: Principal,
        limit: int = 50,
        session_id: str | None = None,
        no_cache: bool = False,
    ) -> FeedResponse:
        url = url.strip()
        if not is_http_url(url):
            raise ApiError(ErrorCode.INVALID_URL, "Only absolute http(s) URLs are supported.")
        session = None
        if session_id:
            if self.sessions is None:
                raise ApiError(ErrorCode.SESSION_NOT_FOUND, "Sessions are not available.")
            session = await self.sessions.resolve(principal, session_id, url=url)
        cache_key = None
        if session is None and not no_cache:
            cache_key = make_key("feed", {"url": canonicalize_url(url)})
            cached = await self.cache.get_model(cache_key, FeedResponse)
            if cached is not None:
                cached.results = cached.results[:limit]
                cached.count = len(cached.results)
                cached.cached = True
                return cached
        result = await self._fetch(url, session)
        info, items = await asyncio.to_thread(parse_feed_bytes, result.content, result.url)
        info.url = url
        response = FeedResponse(feed=info, results=items, count=len(items))
        if cache_key:
            await self.cache.set_model(cache_key, response, self.settings.cache_ttl_feeds)
        response.results = response.results[:limit]
        response.count = len(response.results)
        return response

    async def read_many(self, req: FeedsRequest, principal: Principal) -> FeedsResponse:
        started = time.perf_counter()
        urls = list(dict.fromkeys(u.strip() for u in req.urls))

        async def one(url: str) -> tuple[str, FeedResponse | None, SourceStatus]:
            t0 = time.perf_counter()
            try:
                response = await self.read(
                    url,
                    principal=principal,
                    limit=req.limit_per_feed,
                    session_id=req.session_id,
                    no_cache=req.no_cache,
                )
                status = SourceStatus(name=url, ok=True, count=response.count)
            except ApiError as exc:
                response = None
                status = SourceStatus(
                    name=url, ok=False, error=ErrorInfo(code=exc.code, message=exc.message)
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Feed %s failed", host_of(url))
                response = None
                status = SourceStatus(
                    name=url,
                    ok=False,
                    error=ErrorInfo(code=ErrorCode.SOURCE_ERROR, message=type(exc).__name__),
                )
            status.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return url, response, status

        outcomes = await asyncio.gather(*(one(url) for url in urls))
        feeds = [resp.feed for _, resp, _ in outcomes if resp is not None]
        ranked = [
            (f"feed:{site_of(url)}", resp.results) for url, resp, _ in outcomes if resp is not None
        ]
        merged = merge_results(ranked, title_merge=False)
        since = ensure_utc(req.since)
        if since is not None:
            merged = [r for r in merged if r.published_at is None or r.published_at >= since]
        merged.sort(key=lambda r: r.published_at.timestamp() if r.published_at else 0, reverse=True)
        merged = merged[: req.limit]
        statuses = [status for _, _, status in outcomes]
        failed = [s.name for s in statuses if not s.ok]
        return FeedsResponse(
            feeds=feeds,
            results=merged,
            count=len(merged),
            sources=statuses,
            failed_sources=failed,
            partial=bool(failed),
            took_ms=int((time.perf_counter() - started) * 1000),
        )

    async def discover(self, url: str, *, verify: bool = True) -> DiscoverResponse:
        url = url.strip()
        if not is_http_url(url):
            raise ApiError(ErrorCode.INVALID_URL, "Only absolute http(s) URLs are supported.")
        result = await self._fetch(url)
        content_type = result.content_type
        if (
            content_type in FEED_TYPES
            or content_type.endswith(("+xml", "/xml"))
            or content_type == "application/json"
        ):
            try:
                info, items = parse_feed_bytes(result.content, result.url)
                return DiscoverResponse(
                    url=url,
                    feeds=[
                        DiscoveredFeed(
                            url=result.url,
                            title=info.title,
                            type=info.format,
                            found_by="self",
                            verified=True,
                            item_count=len(items),
                        )
                    ],
                )
            except ApiError:
                pass
        candidates: dict[str, DiscoveredFeed] = {}
        try:
            tree = lxml_html.document_fromstring(result.text)
        except Exception:
            tree = None
        if tree is not None:
            for link in tree.xpath("//link[@href and @rel]"):
                rel = (link.get("rel") or "").lower().split()
                kind = (link.get("type") or "").lower().split(";")[0].strip()
                if "alternate" in rel and (kind in FEED_TYPES or kind == "application/json"):
                    href = urljoin(result.url, link.get("href"))
                    if is_http_url(href) and "/oembed" not in href:
                        candidates.setdefault(
                            href,
                            DiscoveredFeed(
                                url=href,
                                title=collapse_ws(link.get("title")) or None,
                                type=FEED_TYPES.get(kind, "json"),
                                found_by="link",
                            ),
                        )
            if not candidates:
                for anchor in tree.xpath("//a[@href]"):
                    href = urljoin(result.url, anchor.get("href"))
                    lowered = href.lower()
                    if (
                        is_http_url(href)
                        and site_of(href) == site_of(result.url)
                        and re.search(r"(rss|atom|/feed)(\b|[./?])", lowered)
                    ):
                        candidates.setdefault(
                            href,
                            DiscoveredFeed(
                                url=href,
                                title=collapse_ws(anchor.text_content()) or None,
                                found_by="anchor",
                            ),
                        )
                    if len(candidates) >= 10:
                        break
        if not candidates and verify:
            for path in COMMON_FEED_PATHS:
                href = urljoin(result.url, path)
                candidates.setdefault(href, DiscoveredFeed(url=href, found_by="guess"))
        feeds = list(candidates.values())[:12]
        if verify and feeds:

            async def check(feed: DiscoveredFeed) -> DiscoveredFeed:
                try:
                    fetched = await self._fetch(feed.url)
                    info, items = parse_feed_bytes(fetched.content, fetched.url)
                    feed.verified = True
                    feed.item_count = len(items)
                    feed.title = feed.title or info.title
                    feed.type = info.format or feed.type
                except ApiError:
                    feed.verified = False
                return feed

            feeds = list(await asyncio.gather(*(check(f) for f in feeds)))
            feeds = [f for f in feeds if f.verified or f.found_by != "guess"]
        return DiscoverResponse(url=url, feeds=feeds)
