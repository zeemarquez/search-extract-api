"""Resolution of Google News article links (``news.google.com/rss/articles/…``) to the
publisher's URL.

Old links embed the URL in a base64 protobuf and are decoded offline. Current links
need two requests to Google: the article page (for a signature and timestamp) and the
``batchexecute`` endpoint, which answers with the publisher URL. Results are cached.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ...core.cache import Cache
from ...core.http import HttpService
from ...core.urls import is_http_url

logger = logging.getLogger(__name__)

_ARTICLE_PATH = re.compile(r"^/(?:rss/)?(?:articles|read)/([A-Za-z0-9_-]{10,})")
_SIGNATURE = re.compile(r'data-n-a-sg="([^"]+)"')
_TIMESTAMP = re.compile(r'data-n-a-ts="([^"]+)"')
_EMBEDDED_URL = re.compile(rb"(https?://[\x21-\x7e]+)")
BATCHEXECUTE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
CACHE_TTL = 30 * 86400


def article_id(url: str) -> str | None:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "news.google.com":
        return None
    match = _ARTICLE_PATH.match(parts.path)
    return match.group(1) if match else None


def decode_offline(identifier: str) -> str | None:
    """Decodes old-style ids that embed the URL directly."""
    try:
        raw = base64.urlsafe_b64decode(identifier + "=" * (-len(identifier) % 4))
    except (binascii.Error, ValueError):
        return None
    if b"AU_yqL" in raw:  # new format: needs the online decoder
        return None
    match = _EMBEDDED_URL.search(raw)
    if not match:
        return None
    candidate = match.group(1).decode("ascii", "ignore")
    # Protobuf fields may follow the URL; cut at the first byte that ends it.
    candidate = re.split(r"[\x00-\x1f\xd2]", candidate)[0]
    return candidate if is_http_url(candidate) else None


def parse_batchexecute(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            rows = json.loads(line)
        except ValueError:
            continue
        for row in rows:
            if (
                isinstance(row, list)
                and len(row) > 2
                and row[0] == "wrb.fr"
                and isinstance(row[2], str)
            ):
                try:
                    payload: Any = json.loads(row[2])
                except ValueError:
                    continue
                if isinstance(payload, list) and len(payload) > 1 and payload[0] == "garturlres":
                    url = payload[1]
                    return url if is_http_url(url) else None
    return None


class GoogleNewsResolver:
    name = "google_news"

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(4)

    def matches(self, url: str) -> bool:
        return article_id(url) is not None

    async def resolve(self, url: str, http: HttpService, cache: Cache) -> str | None:
        identifier = article_id(url)
        if identifier is None:
            return None
        key = f"gnews:url:{identifier[-120:]}"
        cached = await cache.get(key)
        if cached:
            return cached.decode()
        resolved = decode_offline(identifier)
        if resolved is None:
            async with self._semaphore:
                resolved = await self._decode_online(identifier, http)
        if resolved:
            await cache.set(key, resolved.encode(), CACHE_TTL)
        return resolved

    async def _decode_online(self, identifier: str, http: HttpService) -> str | None:
        headers = {"User-Agent": http.settings.user_agent, "Accept-Language": "en-US,en;q=0.9"}
        try:
            # ucbcb=1 skips the EU cookie-consent interstitial.
            page = await http.client.get(
                f"https://news.google.com/rss/articles/{identifier}",
                params={"ucbcb": "1", "hl": "en-US", "gl": "US", "ceid": "US:en"},
                headers=headers,
                follow_redirects=True,
                timeout=http.settings.http_timeout,
            )
            signature = _SIGNATURE.search(page.text)
            timestamp = _TIMESTAMP.search(page.text)
            if not signature or not timestamp:
                logger.info(
                    "Google News page without decoding parameters (HTTP %s)", page.status_code
                )
                return None
            inner = (
                '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,'
                'null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
                f'"{identifier}",{timestamp.group(1)},"{signature.group(1)}"]'
            )
            body = "f.req=" + quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
            response = await http.client.post(
                BATCHEXECUTE,
                content=body,
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
                timeout=http.settings.http_timeout,
            )
        except httpx.HTTPError as exc:
            logger.info("Google News decoding failed: %s", type(exc).__name__)
            return None
        if response.status_code >= 400:
            return None
        return parse_batchexecute(response.text)
