"""SearXNG adapter (self-hosted metasearch with JSON output)."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from ..config import Settings
from ..core.dates import parse_datetime, time_range_for
from ..core.text import strip_html, truncate
from ..core.urls import is_http_url, site_of
from ..errors import ApiError, ErrorCode
from ..schemas.common import BaseQuery, Result
from .base import SourceAdapter, SourceContext

RESULTS_PER_PAGE = 10


class SearxngAdapter(SourceAdapter):
    """Queries a SearXNG instance. Used for general web search and, with
    ``category="news"``, as a news source."""

    name = "searxng"
    title = "SearXNG"
    description = "Self-hosted metasearch engine aggregating many search engines."

    def __init__(
        self,
        *,
        name: str = "searxng",
        category: str = "general",
        result_model: type[Result] = Result,
        title: str | None = None,
        description: str | None = None,
        extra_terms: str | None = None,
    ) -> None:
        # Instance attributes shadow the class-level defaults.
        self.name = name  # type: ignore[misc]
        self.category = category
        self.result_model = result_model  # type: ignore[misc]
        self.title = title or f"SearXNG ({category})"  # type: ignore[misc]
        if description:
            self.description = description  # type: ignore[misc]
        self.extra_terms = extra_terms

    def configured(self, settings: Settings) -> bool:
        return bool(settings.searxng_url)

    def notes(self, settings: Settings) -> list[str]:
        return [
            "Works best from a residential IP: several engines block data-center IPs.",
        ]

    def _language(self, query: BaseQuery, settings: Settings) -> str:
        if query.lang and query.region:
            return f"{query.lang}-{query.region}"
        if query.lang:
            return query.lang
        return settings.search_default_language or "all"

    def build_query(self, query: BaseQuery) -> str:
        text = query.q or ""
        if self.extra_terms:
            text = f"{text} {self.extra_terms}".strip()
        return text

    async def _page(self, ctx: SourceContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await ctx.http.get_json(
                f"{ctx.settings.searxng_url}/search", params=params, source="SearXNG"
            )
        except ApiError as exc:
            if exc.details.get("status_code") == 403:
                raise ApiError(
                    ErrorCode.SOURCE_ERROR,
                    "SearXNG refused the JSON request: enable 'json' in search.formats.",
                ) from exc
            raise
        if not isinstance(data, dict):
            raise ApiError(ErrorCode.PARSE_ERROR, "SearXNG returned an unexpected payload.")
        return data

    def to_result(self, item: dict[str, Any]) -> Result | None:
        url = item.get("url")
        if not is_http_url(url):
            return None
        image = item.get("img_src") or item.get("thumbnail") or item.get("thumbnail_src")
        engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
        fields: dict[str, Any] = {
            "url": url,
            "title": strip_html(item.get("title")) or None,
            "snippet": truncate(strip_html(item.get("content")), 500) or None,
            "published_at": parse_datetime(item.get("publishedDate") or item.get("pubdate")),
            "image": image if is_http_url(image) else None,
            "source": site_of(url),
            "providers": [self.name],
            "extra": {"engines": engines, "category": item.get("category")},
        }
        author = item.get("author")
        if (
            isinstance(author, str)
            and author.strip()
            and "author" in self.result_model.model_fields
        ):
            fields["author"] = author.strip()
        return self.result_model(**fields)

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        text = self.build_query(query)
        if not text:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "A query (q) is required.")
        time_range = query.time_range or time_range_for(query.since)
        pages = min(
            ctx.settings.searxng_max_pages, max(1, math.ceil(query.limit / RESULTS_PER_PAGE))
        )
        first_page = (query.page - 1) * pages + 1
        base: dict[str, Any] = {
            "q": text,
            "format": "json",
            "categories": self.category,
            "language": self._language(query, ctx.settings),
            "safesearch": getattr(query, "safesearch", 1),
        }
        if time_range:
            base["time_range"] = time_range
        responses = await asyncio.gather(
            *(self._page(ctx, {**base, "pageno": first_page + i}) for i in range(pages)),
            return_exceptions=True,
        )
        if isinstance(responses[0], BaseException):
            raise responses[0]
        results: list[Result] = []
        unresponsive: dict[str, str] = {}
        for response in responses:
            if isinstance(response, BaseException):
                continue
            for engine in response.get("unresponsive_engines") or []:
                if isinstance(engine, list | tuple) and engine:
                    unresponsive[str(engine[0])] = str(engine[1]) if len(engine) > 1 else ""
            for item in response.get("results") or []:
                result = self.to_result(item)
                if result is not None:
                    results.append(result)
        if unresponsive:
            ctx.details["unresponsive_engines"] = unresponsive
        if not results and unresponsive:
            raise ApiError(
                ErrorCode.SOURCE_ERROR,
                "All SearXNG engines failed: "
                + ", ".join(f"{k} ({v})" if v else k for k, v in unresponsive.items()),
                details={"unresponsive_engines": unresponsive},
            )
        return results
