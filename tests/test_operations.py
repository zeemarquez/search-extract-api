"""Operation: API keys, rate limiting, log redaction, cache fallback, cookies, numbers."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest

from search_extract_api.core.cache import Cache, MemoryBackend, make_key
from search_extract_api.core.cookies import Cookie, StorageState, parse_cookies, parse_netscape
from search_extract_api.core.numbers import parse_number
from search_extract_api.core.security import RedactingFilter
from search_extract_api.errors import ApiError

from .conftest import SEARXNG, searxng_payload

pytestmark = pytest.mark.a1


async def test_rate_limit_per_api_key(make_client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(200, json=searxng_payload([])))
    async with make_client(api_keys="key-a,key-b", rate_limit_per_minute=2) as c:
        headers = {"X-API-Key": "key-a"}
        first = await c.get("/search", params={"q": "1"}, headers=headers)
        assert first.headers["x-ratelimit-limit"] == "2"
        assert first.headers["x-ratelimit-remaining"] == "1"
        await c.get("/search", params={"q": "2"}, headers=headers)
        third = await c.get("/search", params={"q": "3"}, headers=headers)
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "RATE_LIMITED"
        assert int(third.headers["retry-after"]) >= 1
        # Limits are per key.
        other = await c.get("/search", params={"q": "4"}, headers={"X-API-Key": "key-b"})
        assert other.status_code == 200


def test_redacting_filter() -> None:
    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "GET /feeds?url=https://a.com/private.xml?token=SECRET123 Cookie: li_at=ABC; JSESSIONID=XYZ "
        "Authorization: Bearer sk-live-123 x-api-key=key-a",
        None,
        None,
    )
    RedactingFilter().filter(record)
    message = record.getMessage()
    for secret in ("SECRET123", "ABC", "XYZ", "sk-live-123", "key-a"):
        assert secret not in message, secret


class FailingBackend:
    """Stands in for Redis: delegates to a dict store unless ``fail`` is set."""

    name = "redis"

    def __init__(self) -> None:
        self.store = MemoryBackend()
        self.fail = True
        self.calls = 0

    async def get(self, key):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        return await self.store.get(key)

    async def set(self, key, value, ttl):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        await self.store.set(key, value, ttl)

    async def ping(self):
        return not self.fail

    async def close(self):
        pass


async def test_cache_falls_back_to_memory_and_recovers() -> None:
    backend = FailingBackend()
    cache = Cache(backend, retry_seconds=0.05)
    await cache.set("k", b"v", 60)  # redis down -> stored in memory fallback
    assert cache.degraded
    assert await cache.get("k") == b"v"
    calls = backend.calls
    await cache.get("k")
    assert backend.calls == calls  # circuit open: no retry storm against redis
    backend.fail = False
    await asyncio.sleep(0.06)
    assert await cache.get("missing") is None
    assert not cache.degraded


async def test_memory_cache_ttl_counters_and_slots() -> None:
    cache = Cache(MemoryBackend())
    await cache.set_json("a", {"x": 1}, 60)
    assert await cache.get_json("a") == {"x": 1}
    assert await cache.incr("c", 60) == 1
    assert await cache.incr("c", 60) == 2
    assert await cache.reserve("slot", 10, 5) == 0
    wait = await cache.reserve("slot", 10, 20)
    assert wait is not None and 9 < wait <= 10
    assert await cache.reserve("slot", 10, 5) is None  # would wait too long
    assert make_key("ns", {"b": 1, "a": 2}) == make_key("ns", {"a": 2, "b": 1})


def test_cookie_matching_rules() -> None:
    state = StorageState(
        cookies=[
            Cookie(name="d", value="1", domain=".example.com", path="/"),
            Cookie(name="h", value="2", domain="www.example.com", path="/"),
            Cookie(name="p", value="3", domain=".example.com", path="/app"),
            Cookie(name="s", value="4", domain=".example.com", secure=True),
            Cookie(name="old", value="5", domain=".example.com", expires=time.time() - 10),
        ]
    )
    assert state.cookie_header("https://www.example.com/app/x") == "p=3; d=1; h=2; s=4"
    assert state.cookie_header("https://api.example.com/") == "d=1; s=4"
    assert state.cookie_header("http://www.example.com/application") == "d=1; h=2"
    assert state.cookie_header("https://example.org/") is None


def test_cookie_formats() -> None:
    netscape = parse_netscape("#HttpOnly_.a.com\tTRUE\t/\tTRUE\t0\tn\tv\n")
    assert netscape[0].httpOnly and netscape[0].domain == ".a.com" and netscape[0].expires == -1
    host_only = parse_cookies([{"name": "a", "value": "b", "domain": "x.com", "hostOnly": True}])
    assert host_only.cookies[0].domain == "x.com"
    domain_cookie = parse_cookies(
        '[{"name": "a", "value": "b", "domain": "x.com", "hostOnly": false, "expirationDate": 1999999999999}]'
    )
    assert domain_cookie.cookies[0].domain == ".x.com"
    assert domain_cookie.cookies[0].expires == pytest.approx(1999999999.999)
    with pytest.raises(ApiError):
        parse_cookies("a\tb\n")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("350.000 €", 350000.0),
        ("1.250,50 €", 1250.5),
        ("1,250.50", 1250.5),
        ("90 m²", 90.0),
        ("90,5 m2", 90.5),
        ("3 hab.", 3.0),
        ("1 200 €", 1200.0),
        ("sin precio", None),
        ("", None),
    ],
)
def test_parse_number(text, value) -> None:
    assert parse_number(text) == value
