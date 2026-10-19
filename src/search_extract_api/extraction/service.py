"""Extraction orchestration: fetch (with fallbacks), detect blocks, extract content,
fingerprint and cache."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..config import Settings
from ..core.cache import Cache, make_key
from ..core.dates import parse_datetime, utcnow
from ..core.http import HttpService
from ..core.lang import detect_language
from ..core.security import Principal
from ..core.text import truncate
from ..core.urls import canonicalize_url, domain_matches, host_of, is_http_url
from ..errors import ApiError, ErrorCode
from ..schemas.extract import (
    BatchExtractRequest,
    BatchExtractResponse,
    BatchItem,
    ExtractRequest,
    ExtractResponse,
    Link,
)
from ..sessions.profiles import profile_for
from .content import (
    clean_html,
    clean_markdown_links,
    extract_links,
    extract_metadata,
    full_markdown,
    main_markdown,
    main_text,
    visible_text_length,
)
from .detection import BlockKind, detect_block
from .fetchers import (
    BrowserFetcher,
    Crawl4AIFetcher,
    FetchedPage,
    Fetcher,
    FetcherUnavailable,
    LightFetcher,
)
from .fingerprint import compute_fingerprint, simhash_similarity
from .resolvers import UrlResolver

if TYPE_CHECKING:
    from ..sessions.browser import BrowserManager
    from ..sessions.models import SessionHandle
    from ..sessions.service import SessionService

logger = logging.getLogger(__name__)


def _words(text: str | None) -> int:
    return len(text.split()) if text else 0


class ExtractionService:
    def __init__(
        self,
        *,
        settings: Settings,
        http: HttpService,
        cache: Cache,
        browser: BrowserManager | None = None,
        sessions: SessionService | None = None,
        resolvers: list[UrlResolver] | None = None,
    ) -> None:
        self.settings = settings
        self.http = http
        self.cache = cache
        self.sessions = sessions
        self.resolvers = list(resolvers or [])
        self.light = LightFetcher(http)
        self.crawl4ai = Crawl4AIFetcher(settings, http.client)
        self.browser = BrowserFetcher(browser)

    # --------------------------------------------------------------------- fetching
    def _chain(self, mode: str, session: SessionHandle | None) -> list[Fetcher]:
        if mode == "light":
            return [self.light]
        if mode == "crawl4ai":
            return [self.crawl4ai]
        if mode == "browser":
            return [self.browser]
        if session is not None:
            return [self.browser, self.light]
        return [self.crawl4ai, self.light] if self.crawl4ai.available() else [self.light]

    async def fetch_page(
        self,
        url: str,
        *,
        session: SessionHandle | None = None,
        mode: str = "auto",
        lang: str | None = None,
    ) -> FetchedPage:
        """Fetches a page through the fetcher chain and raises on login walls, captchas
        and upstream errors. Used by ``/extract`` and by source adapters."""
        url = (url or "").strip()
        if not is_http_url(url):
            raise ApiError(ErrorCode.INVALID_URL, "Only absolute http(s) URLs are supported.")
        url = await self.resolve_url(url)
        await self.http.guard.check(url)
        if session is not None:
            if not domain_matches(host_of(url), session.domain):
                raise ApiError(
                    ErrorCode.SESSION_DOMAIN_MISMATCH,
                    f"Session {session.id} belongs to {session.domain}, not {host_of(url)}.",
                    details={"session_id": session.id, "session_domain": session.domain},
                )
            await self.http.politeness.wait_session(session.id)
        await self.http.politeness.wait_domain(url)

        attempts: list[str] = []
        page: FetchedPage | None = None
        for fetcher in self._chain(mode, session):
            if not fetcher.available():
                attempts.append(f"{fetcher.name}: not available")
                continue
            try:
                page = await fetcher.fetch(url, session=session, lang=lang)
                break
            except FetcherUnavailable as exc:
                logger.info("Fetcher %s unavailable for %s: %s", fetcher.name, host_of(url), exc)
                attempts.append(f"{fetcher.name}: {exc}")
        if page is None:
            code = ErrorCode.BROWSER_UNAVAILABLE if mode == "browser" else ErrorCode.FETCH_FAILED
            raise ApiError(
                code, "No fetcher could retrieve the page.", details={"attempts": attempts}
            )

        await self._check_blocked(page, session)
        if page.status_code == 429:
            raise ApiError(
                ErrorCode.UPSTREAM_RATE_LIMITED,
                "The site is rate limiting requests (HTTP 429).",
                details={"status_code": 429},
            )
        if page.status_code >= 400:
            raise ApiError(
                ErrorCode.UPSTREAM_HTTP_ERROR,
                f"The site returned HTTP {page.status_code}.",
                details={"status_code": page.status_code, "url": page.url},
            )
        if session is not None and self.sessions is not None:
            await self.sessions.record_success(session.id)
        return page

    async def resolve_url(self, url: str) -> str:
        """Replaces redirector links (e.g. Google News) by the original document URL."""
        for resolver in self.resolvers:
            if resolver.matches(url):
                resolved = await resolver.resolve(url, self.http, self.cache)
                if not resolved:
                    raise ApiError(
                        ErrorCode.FETCH_FAILED,
                        f"Could not resolve the {resolver.name} link to the original URL.",
                        details={"url": url},
                    )
                return resolved
        return url

    async def _check_blocked(self, page: FetchedPage, session: SessionHandle | None) -> None:
        detection = detect_block(
            requested_url=page.requested_url,
            final_url=page.url,
            status_code=page.status_code,
            html=page.html,
            main_text_chars=visible_text_length(page.html),
            profile=profile_for(host_of(page.url) or host_of(page.requested_url)),
        )
        if detection is None:
            return
        details: dict[str, object] = {"reason": detection.reason, "url": page.url}
        if detection.kind is BlockKind.BLOCKED:
            raise ApiError(
                ErrorCode.ACCESS_BLOCKED,
                "The site refuses automated access.",
                details={**details, "status_code": page.status_code},
            )
        if session is not None:
            details["session_id"] = session.id
            if detection.kind is BlockKind.CAPTCHA:
                code, message = (
                    ErrorCode.CAPTCHA_REQUIRED,
                    ("The site is showing a captcha / bot challenge for this session."),
                )
            else:
                code, message = (
                    ErrorCode.SESSION_EXPIRED,
                    ("The session is no longer valid: the site asks to log in again."),
                )
            if self.sessions is not None:
                await self.sessions.record_failure(session.id, code, detection.reason)
            raise ApiError(code, message, details=details)
        if detection.kind is BlockKind.CAPTCHA:
            raise ApiError(
                ErrorCode.CAPTCHA_REQUIRED,
                "The site is showing a captcha / bot challenge.",
                details=details,
            )
        raise ApiError(
            ErrorCode.LOGIN_REQUIRED,
            "The page requires logging in. Create a session for this site and pass session_id.",
            details=details,
        )

    # ------------------------------------------------------------------- extraction
    def _build(self, page: FetchedPage, req: ExtractRequest) -> ExtractResponse:
        cleaned = clean_html(page.html)
        text = main_text(cleaned, page.url)
        meta = extract_metadata(page.html, page.url, page.headers)
        c4 = page.metadata or {}
        title = meta.title or c4.get("og:title") or c4.get("title")
        description = meta.description or c4.get("og:description") or c4.get("description")
        author = meta.author or c4.get("author")
        image = meta.image or c4.get("og:image")
        published = meta.published_at or parse_datetime(c4.get("article:published_time"))

        if req.content == "full":
            if page.raw_markdown:
                markdown, extractor = page.raw_markdown, "crawl4ai"
            else:
                markdown, extractor = full_markdown(cleaned), "markdownify"
        else:
            markdown, extractor = main_markdown(cleaned, page.url), "trafilatura"
            if _words(markdown) < 30:
                for candidate, name in (
                    (page.markdown, "crawl4ai"),
                    (page.raw_markdown, "crawl4ai"),
                ):
                    if candidate and _words(candidate) > _words(markdown):
                        markdown, extractor = candidate, name
                        break
            if not markdown and text:
                markdown, extractor = text, "fallback"
        markdown = clean_markdown_links((markdown or "").strip())
        if not markdown and not text:
            raise ApiError(
                ErrorCode.EXTRACTION_FAILED,
                "No readable content could be extracted from the page.",
                details={"url": page.url, "fetcher": page.fetcher},
            )

        max_chars = req.max_chars or self.settings.extract_max_chars
        truncated = len(markdown) > max_chars
        if truncated:
            markdown = truncate(markdown, max_chars) or ""

        links = None
        if req.include_links:
            links = [Link(**item) for item in extract_links(page.html, page.url)]

        return ExtractResponse(
            url=canonicalize_url(page.url),
            requested_url=req.url,
            canonical_url=canonicalize_url(meta.canonical_url) if meta.canonical_url else None,
            status_code=page.status_code,
            title=title,
            author=author,
            published_at=published,
            modified_at=meta.modified_at,
            language=meta.language or detect_language(text or markdown),
            image=image,
            site_name=meta.site_name,
            description=description,
            keywords=meta.keywords,
            content=req.content,
            markdown=markdown,
            word_count=_words(text) if req.content == "main" else _words(markdown),
            truncated=truncated,
            fingerprint=compute_fingerprint(text or markdown),
            links=links,
            fetcher=page.fetcher,
            extractor=extractor,
            fetched_at=utcnow(),
        )

    @staticmethod
    def _compare(response: ExtractResponse, req: ExtractRequest) -> ExtractResponse:
        if req.previous_fingerprint:
            response.changed = req.previous_fingerprint.strip() != response.fingerprint.hash
        if req.previous_simhash:
            response.similarity = simhash_similarity(
                req.previous_simhash, response.fingerprint.simhash
            )
        return response

    async def extract(self, req: ExtractRequest, principal: Principal) -> ExtractResponse:
        started = time.perf_counter()
        session: SessionHandle | None = None
        if req.session_id:
            if self.sessions is None:
                raise ApiError(ErrorCode.SESSION_NOT_FOUND, "Sessions are not available.")
            session = await self.sessions.resolve(principal, req.session_id, url=req.url)
        mode = req.mode or self.settings.extract_default_mode

        cache_key = None
        if session is None and not req.no_cache:
            cache_key = make_key(
                "extract",
                {
                    "url": canonicalize_url(req.url),
                    "mode": mode,
                    "content": req.content,
                    "max_chars": req.max_chars,
                    "lang": req.lang,
                    "links": req.include_links,
                },
            )
            cached = await self.cache.get_model(cache_key, ExtractResponse)
            if cached is not None:
                cached.cached = True
                cached.requested_url = req.url
                cached.elapsed_ms = int((time.perf_counter() - started) * 1000)
                return self._compare(cached, req)

        page = await self.fetch_page(req.url, session=session, mode=mode, lang=req.lang)
        response = await asyncio.to_thread(self._build, page, req)
        response.session_id = session.id if session else None
        if cache_key:
            await self.cache.set_model(cache_key, response, self.settings.cache_ttl_extract)
        response.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return self._compare(response, req)

    async def extract_batch(
        self, req: BatchExtractRequest, principal: Principal
    ) -> BatchExtractResponse:
        semaphore = asyncio.Semaphore(req.concurrency)

        async def one(url: str) -> BatchItem:
            async with semaphore:
                try:
                    single = ExtractRequest(
                        url=url,
                        mode=req.mode,
                        session_id=req.session_id,
                        content=req.content,
                        max_chars=req.max_chars,
                        lang=req.lang,
                        no_cache=req.no_cache,
                    )
                    return BatchItem(url=url, ok=True, result=await self.extract(single, principal))
                except ApiError as exc:
                    return BatchItem(url=url, ok=False, error=exc.to_dict())
                except ValueError as exc:
                    return BatchItem(
                        url=url,
                        ok=False,
                        error={
                            "code": ErrorCode.INVALID_URL.value,
                            "message": str(exc),
                            "details": {},
                        },
                    )

        items = await asyncio.gather(*(one(url) for url in req.urls))
        succeeded = sum(1 for item in items if item.ok)
        return BatchExtractResponse(
            items=list(items), succeeded=succeeded, failed=len(items) - succeeded
        )
