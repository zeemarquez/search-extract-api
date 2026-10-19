"""Optional commercial fallback providers (used only when an API key is configured)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import Settings
from ..core.dates import TIME_RANGES, parse_datetime, resolve_window, utcnow
from ..core.text import strip_html, truncate
from ..core.urls import is_http_url, site_of
from ..errors import ApiError, ErrorCode
from ..schemas.common import BaseQuery, Result
from .base import SourceAdapter, SourceContext


class TavilyAdapter(SourceAdapter):
    name = "tavily"
    title = "Tavily"
    description = "Commercial search API (fallback; needs TAVILY_API_KEY)."
    fallback = True
    optional_config = True

    def __init__(
        self, *, name: str = "tavily", topic: str = "general", result_model: type[Result] = Result
    ) -> None:
        self.name = name  # type: ignore[misc]
        self.topic = topic
        self.result_model = result_model  # type: ignore[misc]

    def configured(self, settings: Settings) -> bool:
        return bool(settings.tavily_api_key)

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        if not query.q:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "A query (q) is required.")
        body: dict[str, Any] = {
            "query": query.q,
            "topic": self.topic,
            "max_results": min(20, query.limit),
            "include_images": False,
            "include_answer": False,
            "include_published_date": True,
        }
        since, _ = resolve_window(query.time_range, query.since, query.until)
        if query.time_range:
            body["time_range"] = query.time_range
        elif since is not None:
            days = max(1, (utcnow() - since).days + 1)
            body["time_range"] = next(
                (name for name, delta in TIME_RANGES.items() if timedelta(days=days) <= delta),
                "year",
            )
        domains = getattr(query, "domains", None)
        if domains:
            body["include_domains"] = domains
        excluded = getattr(query, "exclude_domains", None)
        if excluded:
            body["exclude_domains"] = excluded
        data = await ctx.http.get_json(
            "https://api.tavily.com/search",
            method="POST",
            json_body=body,
            headers={"Authorization": f"Bearer {ctx.settings.tavily_api_key}"},
            source="Tavily",
        )
        results: list[Result] = []
        for item in (data or {}).get("results") or []:
            url = item.get("url")
            if not is_http_url(url):
                continue
            results.append(
                self.result_model(
                    url=url,
                    title=strip_html(item.get("title")) or None,
                    snippet=truncate(strip_html(item.get("content")), 500) or None,
                    published_at=parse_datetime(item.get("published_date")),
                    source=site_of(url),
                    providers=[self.name],
                    extra={"provider_score": item["score"]}
                    if item.get("score") is not None
                    else {},
                )
            )
        return results


class ExaAdapter(SourceAdapter):
    name = "exa"
    title = "Exa"
    description = "Commercial neural search API (fallback; needs EXA_API_KEY)."
    fallback = True
    optional_config = True

    def __init__(
        self, *, name: str = "exa", category: str | None = None, result_model: type[Result] = Result
    ) -> None:
        self.name = name  # type: ignore[misc]
        self.category = category
        self.result_model = result_model  # type: ignore[misc]

    def configured(self, settings: Settings) -> bool:
        return bool(settings.exa_api_key)

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        if not query.q:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "A query (q) is required.")
        body: dict[str, Any] = {
            "query": query.q,
            "numResults": min(25, query.limit),
            "type": "auto",
            "contents": {"highlights": {"maxCharacters": 400}},
        }
        if self.category:
            body["category"] = self.category
        since, until = resolve_window(query.time_range, query.since, query.until)
        if since is not None:
            body["startPublishedDate"] = since.isoformat().replace("+00:00", "Z")
        if until is not None:
            body["endPublishedDate"] = until.isoformat().replace("+00:00", "Z")
        domains = getattr(query, "domains", None)
        if domains:
            body["includeDomains"] = domains
        excluded = getattr(query, "exclude_domains", None)
        if excluded:
            body["excludeDomains"] = excluded
        data = await ctx.http.get_json(
            "https://api.exa.ai/search",
            method="POST",
            json_body=body,
            headers={"x-api-key": ctx.settings.exa_api_key or ""},
            source="Exa",
        )
        results: list[Result] = []
        for item in (data or {}).get("results") or []:
            url = item.get("url")
            if not is_http_url(url):
                continue
            highlights = item.get("highlights") or []
            snippet = highlights[0] if highlights else item.get("summary") or item.get("text")
            fields: dict[str, Any] = {
                "url": url,
                "title": strip_html(item.get("title")) or None,
                "snippet": truncate(strip_html(snippet), 500) or None,
                "published_at": parse_datetime(item.get("publishedDate")),
                "image": item.get("image") if is_http_url(item.get("image")) else None,
                "source": site_of(url),
                "providers": [self.name],
                "extra": {"provider_score": item.get("score")},
            }
            author = item.get("author")
            if author and "author" in self.result_model.model_fields:
                fields["author"] = str(author)
            results.append(self.result_model(**fields))
        return results
