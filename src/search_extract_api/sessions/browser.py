"""Embedded headless browser (Playwright/Chromium).

Used for interactive logins, session health checks that need JavaScript and
rendering of authenticated pages. Playwright is an optional dependency
(``pip install search-extract-api[browser]``, included in the Docker image); without
it these features answer ``503 BROWSER_UNAVAILABLE`` and the rest keeps working.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings
from ..errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


@dataclass
class RenderedPage:
    url: str
    status_code: int
    html: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0


def _is_private_literal(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self._error: str | None = None
        self.enabled = settings.browser_enabled and self._importable()

    @staticmethod
    def _importable() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._browser is not None,
            "error": self._error,
        }

    async def _ensure_browser(self) -> Any:
        if not self.enabled:
            raise ApiError(
                ErrorCode.BROWSER_UNAVAILABLE,
                "The headless browser is not available (install the 'browser' extra "
                "and run 'playwright install chromium', or use the Docker image).",
            )
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.settings.browser_headless,
                    args=[
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                self._error = None
                logger.info("Headless browser started.")
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"[:300]
                logger.warning("Could not start the headless browser: %s", self._error)
                raise ApiError(
                    ErrorCode.BROWSER_UNAVAILABLE, "Could not start the headless browser."
                ) from exc
            return self._browser

    async def new_context(
        self,
        *,
        storage_state: dict[str, Any] | None = None,
        lang: str | None = None,
        viewport: tuple[int, int] = (1280, 800),
        block_heavy: bool = False,
    ) -> Any:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            storage_state=storage_state,
            user_agent=self.settings.user_agent,
            locale=lang or "en-US",
            viewport={"width": viewport[0], "height": viewport[1]},
        )

        async def _route(route: Any) -> None:
            request = route.request
            if not self.settings.allow_private_urls and _is_private_literal(request.url):
                await route.abort()
            elif block_heavy and request.resource_type in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", _route)
        return context

    async def render(
        self, url: str, *, storage_state: dict[str, Any] | None = None, lang: str | None = None
    ) -> RenderedPage:
        started = time.perf_counter()
        context = await self.new_context(storage_state=storage_state, lang=lang, block_heavy=True)
        try:
            page = await context.new_page()
            timeout_ms = int(self.settings.browser_page_timeout * 1000)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                name = type(exc).__name__
                if "Timeout" in name:
                    raise ApiError(ErrorCode.FETCH_TIMEOUT, f"Timed out loading {url}") from exc
                raise ApiError(ErrorCode.FETCH_FAILED, f"Browser could not load {url}") from exc
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass  # long-polling pages never go idle; the DOM is good enough
            html = await page.content()
            headers: dict[str, str] = {}
            status = 200
            if response is not None:
                status = response.status
                try:
                    headers = {k.lower(): v for k, v in (await response.all_headers()).items()}
                except Exception:
                    headers = {}
            return RenderedPage(
                url=page.url,
                status_code=status,
                html=html,
                headers=headers,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            await context.close()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # pragma: no cover - best effort
            pass
        self._browser = None
        self._playwright = None
