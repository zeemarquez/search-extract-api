"""Outgoing HTTP: shared client, SSRF guard, size limits, cookies and polite pacing."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import Settings
from ..errors import ApiError, ErrorCode
from .cache import Cache
from .cookies import StorageState
from .urls import site_of

logger = logging.getLogger(__name__)

_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


@dataclass
class HttpResult:
    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes
    redirects: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def text(self) -> str:
        return decode_body(self.content, self.headers.get("content-type"))


def decode_body(content: bytes, content_type: str | None = None) -> str:
    """Decodes an HTTP body using the header charset, a ``<meta charset>`` or UTF-8."""
    candidates: list[str] = []
    if content_type and "charset=" in content_type.lower():
        candidates.append(content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'"))
    match = _META_CHARSET.search(content[:4096])
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
        if best is not None:
            return str(best)
    except Exception:  # pragma: no cover - optional dependency path
        pass
    return content.decode("utf-8", errors="replace")


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


class UrlGuard:
    """Rejects non-HTTP URLs and (unless allowed) URLs resolving to private networks."""

    def __init__(self, allow_private: bool) -> None:
        self.allow_private = allow_private

    async def check(self, url: str) -> None:
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise ApiError(ErrorCode.INVALID_URL, f"Invalid URL: {exc}") from exc
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ApiError(ErrorCode.INVALID_URL, "Only absolute http(s) URLs are supported.")
        if self.allow_private:
            return
        host = parts.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
            raise ApiError(ErrorCode.BLOCKED_URL, "URLs pointing to local hosts are not allowed.")
        try:
            ips = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                loop = asyncio.get_running_loop()
                infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            except (OSError, UnicodeError) as exc:
                raise ApiError(ErrorCode.FETCH_FAILED, f"Could not resolve host '{host}'.") from exc
            ips = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
        if not ips or not all(_ip_is_public(ip) for ip in ips):
            raise ApiError(
                ErrorCode.BLOCKED_URL,
                "URLs resolving to private, loopback or link-local addresses are not allowed.",
            )


class Politeness:
    """Per-domain and per-session pacing for requests to third-party sites.

    Requests to the same site are spaced by ``domain_min_interval``; requests that use
    an authenticated session are spaced by ``session_min_interval`` plus random jitter
    and capped per hour, to mimic human browsing.
    """

    def __init__(self, cache: Cache, settings: Settings) -> None:
        self.cache = cache
        self.settings = settings

    async def _wait(self, key: str, interval: float, error: ErrorCode, what: str) -> None:
        if interval <= 0:
            return
        wait = await self.cache.reserve(key, interval, self.settings.politeness_max_wait)
        if wait is None:
            raise ApiError(
                error,
                f"Too many requests queued for {what}; try again later.",
                details={"retry_after": int(self.settings.politeness_max_wait)},
            )
        if wait > 0:
            await asyncio.sleep(wait)

    async def wait_domain(self, url: str) -> None:
        site = site_of(url)
        if site:
            await self._wait(
                f"pol:d:{site}",
                self.settings.domain_min_interval,
                ErrorCode.DOMAIN_RATE_LIMITED,
                site,
            )

    async def wait_key(self, key: str, interval: float) -> None:
        await self._wait(f"pol:k:{key}", interval, ErrorCode.DOMAIN_RATE_LIMITED, key)

    async def wait_session(self, session_id: str) -> None:
        cap = self.settings.session_max_requests_per_hour
        if cap > 0:
            hour = int(time.time() // 3600)
            count = await self.cache.incr(f"pol:sq:{session_id}:{hour}", 3700)
            if count > cap:
                raise ApiError(
                    ErrorCode.SESSION_RATE_LIMITED,
                    f"Session request budget of {cap}/hour exhausted.",
                    details={"session_id": session_id, "limit_per_hour": cap},
                )
        interval = self.settings.session_min_interval + random.uniform(
            0, self.settings.session_jitter
        )
        await self._wait(
            f"pol:s:{session_id}", interval, ErrorCode.SESSION_RATE_LIMITED, "this session"
        )


class HttpService:
    """Shared outgoing HTTP client with the guard and helpers used across the service."""

    def __init__(self, settings: Settings, cache: Cache, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.guard = UrlGuard(settings.allow_private_urls)
        self.politeness = Politeness(cache, settings)
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            http2=False,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def browser_headers(self, lang: str | None = None) -> dict[str, str]:
        accept_language = f"{lang},en;q=0.8" if lang else "en-US,en;q=0.9,es;q=0.8"
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": accept_language,
        }

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        cookies: StorageState | None = None,
        guarded: bool = True,
        timeout: float | None = None,
        max_bytes: int | None = None,
        follow_redirects: bool = True,
        browser_like: bool = True,
    ) -> HttpResult:
        """Fetches ``url`` following redirects manually so every hop is checked by the
        SSRF guard and receives only the session cookies that match it."""
        max_bytes = max_bytes or self.settings.max_fetch_bytes
        base_headers = (
            self.browser_headers()
            if browser_like
            else {
                "User-Agent": self.settings.api_user_agent,
                "Accept": "application/json, application/xml;q=0.9, */*;q=0.5",
            }
        )
        if headers:
            base_headers.update(headers)
        redirects: list[str] = []
        current = url
        current_params = params
        started = time.perf_counter()
        for _ in range(self.settings.max_redirects + 1):
            if guarded:
                await self.guard.check(current)
            hop_headers = dict(base_headers)
            if cookies is not None:
                header = cookies.cookie_header(current)
                if header:
                    hop_headers["Cookie"] = header
            try:
                request = self.client.build_request(
                    method,
                    current,
                    headers=hop_headers,
                    params=current_params,
                    timeout=timeout or self.settings.http_timeout,
                )
                response = await self.client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise ApiError(ErrorCode.FETCH_TIMEOUT, f"Timed out fetching {current}") from exc
            except httpx.HTTPError as exc:
                raise ApiError(
                    ErrorCode.FETCH_FAILED, f"Could not fetch {current}: {type(exc).__name__}"
                ) from exc
            try:
                if follow_redirects and response.is_redirect and "location" in response.headers:
                    redirects.append(current)
                    current = urljoin(str(response.url), response.headers["location"])
                    current_params = None
                    if method == "POST" and response.status_code in (301, 302, 303):
                        method = "GET"
                    continue
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ApiError(
                        ErrorCode.CONTENT_TOO_LARGE,
                        f"Response larger than {max_bytes} bytes.",
                        details={"content_length": int(declared)},
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ApiError(
                            ErrorCode.CONTENT_TOO_LARGE, f"Response larger than {max_bytes} bytes."
                        )
                    chunks.append(chunk)
            except httpx.TimeoutException as exc:
                raise ApiError(ErrorCode.FETCH_TIMEOUT, f"Timed out reading {current}") from exc
            except httpx.HTTPError as exc:
                raise ApiError(
                    ErrorCode.FETCH_FAILED, f"Error reading {current}: {type(exc).__name__}"
                ) from exc
            finally:
                await response.aclose()
            return HttpResult(
                url=str(response.url),
                status_code=response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                redirects=redirects,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        raise ApiError(ErrorCode.FETCH_FAILED, f"Too many redirects fetching {url}")

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        json_body: Any = None,
        timeout: float | None = None,
        source: str = "upstream",
    ) -> Any:
        """Calls a JSON API (search engines, LLMs...). Errors map to ``SOURCE_*`` codes."""
        request_headers = {"User-Agent": self.settings.api_user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        try:
            response = await self.client.request(
                method,
                url,
                params=params,
                headers=request_headers,
                json=json_body,
                timeout=timeout or self.settings.http_timeout,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise ApiError(ErrorCode.SOURCE_TIMEOUT, f"{source} timed out") from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                ErrorCode.SOURCE_ERROR, f"{source} unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise ApiError(ErrorCode.UPSTREAM_RATE_LIMITED, f"{source} rate limited the request")
        if response.status_code >= 400:
            raise ApiError(
                ErrorCode.SOURCE_ERROR,
                f"{source} returned HTTP {response.status_code}",
                details={"status_code": response.status_code, "body": response.text[:300]},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                ErrorCode.PARSE_ERROR,
                f"{source} returned a non-JSON response",
                details={"body": response.text[:300]},
            ) from exc
