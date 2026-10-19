"""News vertical: ``/news/search``, ``/news/top`` and ``/news/sources``."""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from ...adapters.base import SourceAdapter
from ...adapters.commercial import ExaAdapter, TavilyAdapter
from ...adapters.searxng import SearxngAdapter
from ...config import Settings
from ...core.cache import make_key
from ...core.lang import normalize_language
from ...core.security import Principal, require_principal
from ...core.urls import domain_matches
from ...errors import ApiError
from ...extraction.resolvers import UrlResolver
from ...schemas.common import BaseQuery, ErrorInfo, Result, SearchResponse, SourceStatus
from ..base import Vertical
from .google_news import GoogleNewsResolver
from .schemas import NewsQuery, NewsResult, NewsTopic
from .sources import GdeltAdapter, GoogleNewsAdapter, google_news_top

__all__ = ["NewsQuery", "NewsResult", "NewsVertical"]


class NewsVertical(Vertical):
    name = "news"
    title = "News"
    description = (
        "News search across SearXNG (news category), GDELT DOC 2.0 and Google News RSS, "
        "with Tavily / Exa as optional fallbacks."
    )
    prefix = "/news"
    query_model = NewsQuery
    result_model = NewsResult

    def __init__(self) -> None:
        self.google_news = GoogleNewsResolver()

    def url_resolvers(self) -> list[UrlResolver]:
        return [self.google_news]

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [
            SearxngAdapter(
                name="searxng_news",
                category="news",
                result_model=NewsResult,
                title="SearXNG (news)",
                description="News engines aggregated by SearXNG.",
            ),
            GdeltAdapter(),
            GoogleNewsAdapter(self.google_news),
            TavilyAdapter(name="tavily_news", topic="news", result_model=NewsResult),
            ExaAdapter(name="exa_news", category="news", result_model=NewsResult),
        ]

    def postprocess(self, results: list[Result], query: BaseQuery) -> list[Result]:
        include = getattr(query, "domains", None) or []
        exclude = getattr(query, "exclude_domains", None) or []
        kept: list[Result] = []
        for result in results:
            site = result.source or ""
            if include and not any(domain_matches(site, d) for d in include):
                continue
            if exclude and any(domain_matches(site, d) for d in exclude):
                continue
            if isinstance(result, NewsResult) and not result.outlet and result.source:
                result.outlet = result.source
            kept.append(result)
        return kept

    def extra_routes(self, router: APIRouter) -> None:
        vertical = self

        @router.get(
            "/top",
            response_model=SearchResponse[NewsResult],
            summary="News: top headlines",
            description="Top stories (optionally for one topic) from Google News for a "
            "language/region edition.",
            operation_id="news_top",
        )
        async def top_headlines(
            request: Request,
            lang: Annotated[str | None, Query(description="ISO 639-1, e.g. 'es'")] = None,
            region: Annotated[
                str | None, Query(description="ISO 3166-1 alpha-2, e.g. 'ES'")
            ] = None,
            topic: NewsTopic | None = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 30,
            resolve_links: Annotated[
                bool, Query(description="Resolve Google News links to publisher URLs.")
            ] = False,
            no_cache: bool = False,
            principal: Principal = Depends(require_principal),
        ) -> SearchResponse[NewsResult]:
            started = time.perf_counter()
            state = request.app.state
            language = normalize_language(lang)
            country = region.upper() if region else None
            cache_key = make_key(
                "news:top",
                {"lang": language, "region": country, "topic": topic, "resolve": resolve_links},
            )
            if not no_cache:
                cached = await state.cache.get_model(cache_key, SearchResponse[NewsResult])
                if cached is not None:
                    cached.cached = True
                    cached.results = cached.results[:limit]
                    cached.count = len(cached.results)
                    return cached
            status = SourceStatus(name="google_news", ok=True)
            results: list[NewsResult] = []
            try:
                results = await google_news_top(
                    state.search.context(None),
                    lang=language,
                    region=country,
                    topic=topic,
                    resolver=vertical.google_news,
                    resolve=limit if resolve_links else 0,
                )
                status.count = len(results)
            except ApiError as exc:
                status = SourceStatus(
                    name="google_news",
                    ok=False,
                    error=ErrorInfo(code=exc.code, message=exc.message),
                )
            status.elapsed_ms = int((time.perf_counter() - started) * 1000)
            results = vertical.postprocess(results, BaseQuery())  # type: ignore[arg-type]
            response = SearchResponse[NewsResult](
                query={"lang": language, "region": country, "topic": topic},
                results=results,
                count=len(results),
                sources=[status],
                failed_sources=[] if status.ok else ["google_news"],
                partial=not status.ok,
                took_ms=status.elapsed_ms,
            )
            if status.ok:
                await state.cache.set_model(cache_key, response, state.settings.cache_ttl_search)
            response.results = response.results[:limit]
            response.count = len(response.results)
            return response
