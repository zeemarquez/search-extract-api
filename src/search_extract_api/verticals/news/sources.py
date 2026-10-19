"""News sources: SearXNG news category, GDELT DOC 2.0 and Google News RSS."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import feedparser

from ...adapters.base import SourceAdapter, SourceContext
from ...config import Settings
from ...core.dates import parse_datetime, resolve_window, utcnow
from ...core.lang import ISO_TO_NAME, normalize_language
from ...core.text import split_title_outlet, strip_html, truncate
from ...core.urls import canonicalize_url, is_http_url, normalize_domain, result_id, site_of
from ...errors import ApiError, ErrorCode
from ...schemas.common import BaseQuery, Result
from .google_news import GoogleNewsResolver
from .schemas import NewsResult

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_URL = "https://news.google.com/rss"

# ISO 3166-1 alpha-2 -> GDELT country names (spaces removed).
GDELT_COUNTRIES = {
    "ES": "spain",
    "US": "unitedstates",
    "GB": "unitedkingdom",
    "UK": "unitedkingdom",
    "FR": "france",
    "DE": "germany",
    "IT": "italy",
    "PT": "portugal",
    "MX": "mexico",
    "AR": "argentina",
    "CL": "chile",
    "CO": "colombia",
    "PE": "peru",
    "VE": "venezuela",
    "BR": "brazil",
    "CA": "canada",
    "IE": "ireland",
    "NL": "netherlands",
    "BE": "belgium",
    "CH": "switzerland",
    "AT": "austria",
    "SE": "sweden",
    "NO": "norway",
    "DK": "denmark",
    "FI": "finland",
    "PL": "poland",
    "JP": "japan",
    "CN": "china",
    "IN": "india",
    "RU": "russia",
    "UA": "ukraine",
    "AU": "australia",
    "NZ": "newzealand",
    "ZA": "southafrica",
    "MA": "morocco",
    "TR": "turkey",
    "GR": "greece",
    "IL": "israel",
    "EG": "egypt",
    "UY": "uruguay",
    "EC": "ecuador",
    "BO": "bolivia",
    "PY": "paraguay",
    "CU": "cuba",
}
# Default Google News edition per language.
GOOGLE_NEWS_DEFAULT_REGION = {
    "es": "ES",
    "en": "US",
    "fr": "FR",
    "de": "DE",
    "it": "IT",
    "pt": "PT",
    "nl": "NL",
    "ca": "ES",
    "ja": "JP",
    "zh": "CN",
    "ru": "RU",
    "pl": "PL",
    "sv": "SE",
}


def _domains(query: BaseQuery) -> tuple[list[str], list[str]]:
    return list(getattr(query, "domains", None) or []), list(
        getattr(query, "exclude_domains", None) or []
    )


class GdeltAdapter(SourceAdapter):
    """GDELT DOC 2.0 article search (free, global, ~3 months of coverage)."""

    name = "gdelt"
    title = "GDELT DOC 2.0"
    description = "Global Database of Events, Language and Tone: worldwide news coverage."
    result_model = NewsResult
    # GDELT is often slow (10-20 s) and throttles aggressively.
    timeout = 40.0

    def configured(self, settings: Settings) -> bool:
        return settings.gdelt_enabled

    def notes(self, settings: Settings) -> list[str]:
        return [
            "GDELT allows about one request every 5 seconds and often answers slowly or "
            "with HTTP 429 under load; failures are reported per source.",
        ]

    @staticmethod
    def build_query(query: BaseQuery) -> str:
        text = (query.q or "").strip()
        if not text:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "A query (q) is required.")
        parts = [text]
        if query.lang:
            parts.append(f"sourcelang:{ISO_TO_NAME.get(query.lang, query.lang)}")
        if query.region:
            parts.append(f"sourcecountry:{GDELT_COUNTRIES.get(query.region, query.region.lower())}")
        include, exclude = _domains(query)
        if include:
            joined = " OR ".join(f"domainis:{d}" for d in include)
            parts.append(f"({joined})" if len(include) > 1 else joined)
        parts.extend(f"-domainis:{d}" for d in exclude)
        return " ".join(parts)

    @staticmethod
    def _timespan(query: BaseQuery) -> dict[str, str]:
        since, until = resolve_window(query.time_range, query.since, query.until)
        if since is None and until is None:
            return {}
        params: dict[str, str] = {}
        if since is not None:
            params["startdatetime"] = since.strftime("%Y%m%d%H%M%S")
        if until is not None:
            params["enddatetime"] = until.strftime("%Y%m%d%H%M%S")
        elif since is not None:
            params["enddatetime"] = utcnow().strftime("%Y%m%d%H%M%S")
        return params

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        params = {
            "query": self.build_query(query),
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(min(250, max(query.limit * 2, 50))),
            "sort": "DateDesc" if query.sort == "date" else "HybridRel",
            **self._timespan(query),
        }
        await ctx.http.politeness.wait_key("gdelt", ctx.settings.gdelt_min_interval)
        result = await ctx.http.fetch(
            GDELT_URL,
            params=params,
            guarded=False,
            browser_like=False,
            timeout=35.0,
        )
        if result.status_code == 429:
            raise ApiError(ErrorCode.UPSTREAM_RATE_LIMITED, "GDELT rate limited the request.")
        if result.status_code >= 400:
            raise ApiError(ErrorCode.SOURCE_ERROR, f"GDELT returned HTTP {result.status_code}.")
        body = result.text.strip()
        if not body:
            return []
        try:
            data = json.loads(body)
        except ValueError:
            # GDELT answers errors (short keywords, bad operators, rate limits) as text.
            raise ApiError(
                ErrorCode.SOURCE_ERROR, f"GDELT: {truncate(strip_html(body), 200)}"
            ) from None
        results: list[Result] = []
        for article in (data or {}).get("articles") or []:
            url = article.get("url")
            if not is_http_url(url):
                continue
            domain = article.get("domain") or site_of(url)
            results.append(
                NewsResult(
                    url=url,
                    title=strip_html(article.get("title")) or None,
                    source=normalize_domain(domain) or site_of(url),
                    published_at=parse_datetime(article.get("seendate")),
                    language=normalize_language(article.get("language")),
                    image=article.get("socialimage")
                    if is_http_url(article.get("socialimage"))
                    else None,
                    providers=[self.name],
                    extra={"country": article.get("sourcecountry")}
                    if article.get("sourcecountry")
                    else {},
                )
            )
        return results


def google_news_edition(lang: str | None, region: str | None) -> dict[str, str]:
    language = lang or "en"
    country = region or GOOGLE_NEWS_DEFAULT_REGION.get(language, "US")
    hl = f"{language}-{country}" if language in {"en", "pt", "zh"} else language
    return {"hl": hl, "gl": country, "ceid": f"{country}:{language}"}


def parse_google_news(content: bytes, provider: str = "google_news") -> list[NewsResult]:
    parsed = feedparser.parse(content)
    results: list[NewsResult] = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not is_http_url(link):
            continue
        source = entry.get("source") or {}
        source_href = source.get("href") if isinstance(source, dict) else None
        source_title = source.get("title") if isinstance(source, dict) else None
        headline, outlet = split_title_outlet(strip_html(entry.get("title")))
        if source_title and headline.endswith(f" - {source_title}"):
            headline = headline[: -len(f" - {source_title}")]
        results.append(
            NewsResult(
                url=link,
                title=headline or None,
                outlet=source_title or outlet,
                source=site_of(source_href) if source_href else None,
                published_at=parse_datetime(
                    entry.get("published_parsed") or entry.get("published")
                ),
                providers=[provider],
                extra={"google_news_url": link},
            )
        )
    return results


async def resolve_links(
    results: list[NewsResult], resolver: GoogleNewsResolver, ctx: SourceContext, limit: int
) -> None:
    """Replaces Google News redirect links by publisher URLs (first ``limit`` results)."""
    targets = [r for r in results[:limit] if resolver.matches(r.url)]
    resolved = await asyncio.gather(
        *(resolver.resolve(r.url, ctx.http, ctx.cache) for r in targets), return_exceptions=True
    )
    for result, url in zip(targets, resolved, strict=True):
        if isinstance(url, str) and url:
            result.extra["google_news_url"] = result.url
            result.url = canonicalize_url(url)
            result.id = result_id(result.url)
            result.source = result.source or site_of(result.url)


class GoogleNewsAdapter(SourceAdapter):
    """Google News search RSS (free, no key). Links are Google redirect URLs; results
    are matched to the same article from other sources by headline and outlet, and can
    be resolved to publisher URLs with ``resolve_links=true``."""

    name = "google_news"
    title = "Google News RSS"
    description = "Google News search feed."
    result_model = NewsResult

    def __init__(self, resolver: GoogleNewsResolver | None = None) -> None:
        self.resolver = resolver or GoogleNewsResolver()

    def configured(self, settings: Settings) -> bool:
        return settings.google_news_enabled

    @staticmethod
    def build_query(query: BaseQuery) -> str:
        text = (query.q or "").strip()
        if not text:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "A query (q) is required.")
        parts = [text]
        since, until = resolve_window(query.time_range, query.since, query.until)
        if since is not None and until is None:
            hours = max(1, int((utcnow() - since).total_seconds() // 3600) + 1)
            parts.append(f"when:{hours}h" if hours <= 72 else f"when:{(hours + 23) // 24}d")
        else:
            if since is not None:
                parts.append(f"after:{since.date().isoformat()}")
            if until is not None:
                parts.append(f"before:{(until + timedelta(days=1)).date().isoformat()}")
        include, exclude = _domains(query)
        if include:
            parts.append("(" + " OR ".join(f"site:{d}" for d in include) + ")")
        parts.extend(f"-site:{d}" for d in exclude)
        return " ".join(parts)

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        params = {"q": self.build_query(query), **google_news_edition(query.lang, query.region)}
        result = await ctx.http.fetch(f"{GOOGLE_NEWS_URL}/search", params=params, guarded=False)
        if result.status_code >= 400:
            raise ApiError(
                ErrorCode.SOURCE_ERROR, f"Google News returned HTTP {result.status_code}."
            )
        results = await asyncio.to_thread(parse_google_news, result.content, self.name)
        if getattr(query, "resolve_links", False):
            await resolve_links(results, self.resolver, ctx, query.limit)
        return results


GOOGLE_NEWS_TOPICS = {
    "world": "WORLD",
    "nation": "NATION",
    "business": "BUSINESS",
    "technology": "TECHNOLOGY",
    "entertainment": "ENTERTAINMENT",
    "sports": "SPORTS",
    "science": "SCIENCE",
    "health": "HEALTH",
}


async def google_news_top(
    ctx: SourceContext,
    *,
    lang: str | None,
    region: str | None,
    topic: str | None,
    resolver: GoogleNewsResolver | None = None,
    resolve: int = 0,
) -> list[NewsResult]:
    edition = google_news_edition(lang, region)
    if topic:
        url = f"{GOOGLE_NEWS_URL}/headlines/section/topic/{GOOGLE_NEWS_TOPICS[topic]}"
    else:
        url = GOOGLE_NEWS_URL
    result = await ctx.http.fetch(url, params=edition, guarded=False)
    if result.status_code >= 400:
        raise ApiError(ErrorCode.SOURCE_ERROR, f"Google News returned HTTP {result.status_code}.")
    results = await asyncio.to_thread(parse_google_news, result.content, "google_news")
    if resolver is not None and resolve > 0:
        await resolve_links(results, resolver, ctx, resolve)
    return results
