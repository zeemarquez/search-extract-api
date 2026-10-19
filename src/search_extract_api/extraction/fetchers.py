"""Page fetchers: Crawl4AI service, headless browser (Playwright) and a lightweight
HTTP fetcher. They all return a :class:`FetchedPage`; content extraction is common."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from ..config import Settings
from ..core.http import HttpService
from ..errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..sessions.browser import BrowserManager
    from ..sessions.models import SessionHandle

logger = logging.getLogger(__name__)

HTML_TYPES = {"text/html", "application/xhtml+xml", ""}
TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}


@dataclass
class FetchedPage:
    url: str
    requested_url: str
    status_code: int
    html: str
    fetcher: str
    content_type: str = "text/html"
    markdown: str | None = None  # fetcher-provided main-content markdown (Crawl4AI fit)
    raw_markdown: str | None = None  # fetcher-provided full-page markdown
    metadata: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    redirects: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


def _looks_like_html(text: str) -> bool:
    head = text[:1024].lstrip().lower()
    return head.startswith(("<!doctype html", "<html", "<head", "<body"))


class FetcherUnavailable(Exception):
    """The fetcher itself failed (service down, browser missing): try the next one."""


class Fetcher:
    name = "base"

    def available(self) -> bool:
        return True

    async def fetch(
        self, url: str, *, session: SessionHandle | None = None, lang: str | None = None
    ) -> FetchedPage:  # pragma: no cover - interface
        raise NotImplementedError


class LightFetcher(Fetcher):
    """Plain HTTP GET with browser-like headers (and session cookies if given)."""

    name = "light"

    def __init__(self, http: HttpService) -> None:
        self.http = http

    async def fetch(
        self, url: str, *, session: SessionHandle | None = None, lang: str | None = None
    ) -> FetchedPage:
        headers = self.http.browser_headers(lang)
        result = await self.http.fetch(
            url, headers=headers, cookies=session.state if session else None
        )
        if result.status_code in (403, 429) and session is None and len(result.content) < 20_000:
            # Some sites (e.g. Wikipedia) reject a browser User-Agent whose TLS fingerprint
            # is not a browser's, but welcome an honest crawler identity. Try that once.
            honest = {**headers, "User-Agent": self.http.settings.api_user_agent}
            retry = await self.http.fetch(url, headers=honest)
            if retry.status_code < 400:
                result = retry
        content_type = result.content_type
        if content_type not in HTML_TYPES | TEXT_TYPES and not content_type.endswith("+xml"):
            raise ApiError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"Unsupported content type '{content_type}'.",
                details={"content_type": content_type, "status_code": result.status_code},
            )
        text = result.text
        if content_type in TEXT_TYPES and not _looks_like_html(text):
            from html import escape

            text = f"<html><body><pre>{escape(text)}</pre></body></html>"
        return FetchedPage(
            url=result.url,
            requested_url=url,
            status_code=result.status_code,
            html=text,
            fetcher=self.name,
            content_type=content_type or "text/html",
            headers={k.lower(): v for k, v in result.headers.items()},
            redirects=result.redirects,
            elapsed_ms=result.elapsed_ms,
        )


class Crawl4AIFetcher(Fetcher):
    """Renders pages with a Crawl4AI server (headless Chromium) through its REST API."""

    name = "crawl4ai"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    def available(self) -> bool:
        return bool(self.settings.crawl4ai_url)

    def _payload(self, url: str, content_filter: str) -> dict[str, Any]:
        # Crawl4AI >= 0.9 rejects cookies/storage_state/headers in REST requests, so the
        # payload only carries rendering options. Authenticated pages never come here.
        return {
            "urls": [url],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True, "text_mode": True},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "cache_mode": "bypass",
                    "page_timeout": int(self.settings.crawl4ai_timeout * 1000),
                    "wait_until": "domcontentloaded",
                    "remove_overlay_elements": True,
                    "markdown_generator": {
                        "type": "DefaultMarkdownGenerator",
                        "params": {
                            "content_filter": {
                                "type": content_filter,
                                "params": {
                                    "threshold": 0.48,
                                    "threshold_type": "fixed",
                                    "min_word_threshold": 0,
                                },
                            }
                        },
                    },
                },
            },
        }

    async def _post(self, url: str, content_filter: str) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if self.settings.crawl4ai_token:
            headers["Authorization"] = f"Bearer {self.settings.crawl4ai_token}"
        try:
            return await self.client.post(
                f"{self.settings.crawl4ai_url}/crawl",
                json=self._payload(url, content_filter),
                headers=headers,
                timeout=self.settings.crawl4ai_timeout + 15,
            )
        except httpx.HTTPError as exc:
            raise FetcherUnavailable(f"crawl4ai unreachable: {type(exc).__name__}") from exc

    async def fetch(
        self, url: str, *, session: SessionHandle | None = None, lang: str | None = None
    ) -> FetchedPage:
        if not self.available():
            raise FetcherUnavailable("crawl4ai not configured")
        if session is not None:
            # Session cookies are never sent to another service; the headless browser
            # embedded in this API handles authenticated pages.
            raise FetcherUnavailable("crawl4ai is not used for authenticated sessions")
        started = time.perf_counter()
        response = await self._post(url, "PruningContentFilterLXML")
        if response.status_code == 400:
            # Older Crawl4AI servers do not know the LXML variant of the filter.
            response = await self._post(url, "PruningContentFilter")
        if response.status_code in (401, 403):
            raise FetcherUnavailable("crawl4ai rejected the token (check CRAWL4AI_TOKEN)")
        if response.status_code >= 400:
            raise FetcherUnavailable(f"crawl4ai returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise FetcherUnavailable("crawl4ai returned invalid JSON") from exc
        results = data.get("results") or []
        if not results:
            raise FetcherUnavailable("crawl4ai returned no results")
        result = results[0]
        status_code = int(result.get("status_code") or 0)
        html = result.get("html") or result.get("cleaned_html") or ""
        if not result.get("success", True) and not html:
            message = str(result.get("error_message") or "crawl failed")[:300]
            if status_code >= 400:
                raise ApiError(
                    ErrorCode.UPSTREAM_HTTP_ERROR,
                    f"The site returned HTTP {status_code}.",
                    details={"status_code": status_code},
                )
            raise FetcherUnavailable(f"crawl4ai: {message}")
        markdown = result.get("markdown")
        fit_md = raw_md = None
        if isinstance(markdown, dict):
            fit_md = markdown.get("fit_markdown") or None
            raw_md = markdown.get("raw_markdown") or None
        elif isinstance(markdown, str):
            raw_md = markdown
        metadata = result.get("metadata") or {}
        response_headers = result.get("response_headers") or {}
        return FetchedPage(
            url=result.get("redirected_url") or result.get("url") or url,
            requested_url=url,
            status_code=status_code or 200,
            html=html,
            fetcher=self.name,
            markdown=fit_md,
            raw_markdown=raw_md,
            metadata=metadata if isinstance(metadata, dict) else {},
            headers={str(k).lower(): str(v) for k, v in response_headers.items()}
            if isinstance(response_headers, dict)
            else {},
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


class BrowserFetcher(Fetcher):
    """Renders pages in the embedded headless browser, with the session's full storage
    state (cookies + localStorage) when one is given."""

    name = "browser"

    def __init__(self, browser: BrowserManager | None) -> None:
        self.browser = browser

    def available(self) -> bool:
        return self.browser is not None and self.browser.enabled

    async def fetch(
        self, url: str, *, session: SessionHandle | None = None, lang: str | None = None
    ) -> FetchedPage:
        if not self.available() or self.browser is None:
            raise FetcherUnavailable("headless browser not available")
        state = session.state.model_dump() if session else None
        try:
            rendered = await self.browser.render(url, storage_state=state, lang=lang)
        except ApiError as exc:
            if exc.code == ErrorCode.BROWSER_UNAVAILABLE:
                raise FetcherUnavailable(exc.message) from exc
            raise
        return FetchedPage(
            url=rendered.url,
            requested_url=url,
            status_code=rendered.status_code,
            html=rendered.html,
            fetcher=self.name,
            headers=rendered.headers,
            elapsed_ms=rendered.elapsed_ms,
        )
