"""Temporary cache (Redis/Valkey or in-memory) and the counters built on top of it.

The cache is the only place where fetched content lives, always with a TTL. If Redis
is not configured or becomes unreachable, every operation transparently falls back
to a per-process in-memory store, so the service keeps working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

_RESERVE_LUA = """
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local max_wait = tonumber(ARGV[3])
local next_free = tonumber(redis.call('GET', KEYS[1]) or '0')
local start = now
if next_free > now then start = next_free end
local wait = start - now
if wait > max_wait then return {0, tostring(wait)} end
redis.call('SET', KEYS[1], tostring(start + interval), 'PX', math.ceil((wait + interval) * 1000) + 1000)
return {1, tostring(wait)}
"""


class CacheBackend(Protocol):
    name: str

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, ttl: int) -> int: ...
    async def reserve(self, key: str, interval: float, max_wait: float) -> float | None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class MemoryBackend:
    """Bounded in-memory TTL store (per process)."""

    name = "memory"

    def __init__(self, max_items: int = 5000) -> None:
        self._data: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._counters: dict[str, tuple[float, int]] = {}
        self._slots: dict[str, float] = {}
        self._max_items = max_items

    def _expire(self) -> None:
        now = time.monotonic()
        if len(self._counters) > 10_000:
            self._counters = {k: v for k, v in self._counters.items() if v[0] > now}
        if len(self._slots) > 10_000:
            wall = time.time()
            self._slots = {k: v for k, v in self._slots.items() if v > wall}

    async def get(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_items:
            self._data.popitem(last=False)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        now = time.monotonic()
        expires, count = self._counters.get(key, (0.0, 0))
        if expires < now:
            expires, count = now + ttl, 0
        count += 1
        self._counters[key] = (expires, count)
        self._expire()
        return count

    async def reserve(self, key: str, interval: float, max_wait: float) -> float | None:
        now = time.time()
        start = max(now, self._slots.get(key, 0.0))
        wait = start - now
        if wait > max_wait:
            return None
        self._slots[key] = start + interval
        return wait

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


class RedisBackend:
    name = "redis"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis_asyncio

        self._client = redis_asyncio.from_url(
            url, socket_connect_timeout=2, socket_timeout=2, health_check_interval=30
        )
        self._reserve = self._client.register_script(_RESERVE_LUA)

    async def get(self, key: str) -> bytes | None:
        return await self._client.get(key)

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        await self._client.set(key, value, ex=max(1, int(ttl)))

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def incr(self, key: str, ttl: int) -> int:
        count = int(await self._client.incr(key))
        if count == 1:
            await self._client.expire(key, max(1, int(ttl)))
        return count

    async def reserve(self, key: str, interval: float, max_wait: float) -> float | None:
        allowed, wait = await self._reserve(keys=[key], args=[time.time(), interval, max_wait])
        if int(allowed) != 1:
            return None
        return float(wait)

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def close(self) -> None:
        await self._client.aclose()


class Cache:
    """Namespaced JSON cache with transparent fallback to memory on backend errors.

    A small circuit breaker avoids paying the connection timeout on every call while
    the remote backend is down: after an error the fallback is used for
    ``retry_seconds`` before the remote backend is tried again.
    """

    def __init__(
        self,
        backend: CacheBackend,
        *,
        prefix: str = "sea:",
        max_items: int = 5000,
        retry_seconds: float = 15.0,
    ):
        self.backend = backend
        self.prefix = prefix
        self._fallback = backend if isinstance(backend, MemoryBackend) else MemoryBackend(max_items)
        self._degraded = False
        self._retry_at = 0.0
        self._retry_seconds = retry_seconds

    @classmethod
    async def create(cls, redis_url: str | None, *, max_items: int = 5000) -> Cache:
        if not redis_url:
            return cls(MemoryBackend(max_items), max_items=max_items)
        cache = cls(RedisBackend(redis_url), max_items=max_items)
        if await cache.ping():
            logger.info("Cache: using Redis/Valkey at %s", redis_url.split("@")[-1])
        else:
            logger.warning("Cache: Redis/Valkey unreachable; using in-memory fallback for now.")
            cache._trip()
        return cache

    @property
    def backend_name(self) -> str:
        return "memory (redis unreachable)" if self._degraded else self.backend.name

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _k(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def _trip(self) -> None:
        self._degraded = True
        self._retry_at = time.monotonic() + self._retry_seconds

    async def _call(self, method: str, *args: Any) -> Any:
        if self.backend is self._fallback:
            return await getattr(self.backend, method)(*args)
        if self._degraded and time.monotonic() < self._retry_at:
            return await getattr(self._fallback, method)(*args)
        try:
            result = await getattr(self.backend, method)(*args)
        except Exception as exc:
            if not self._degraded:
                logger.warning("Cache backend error (%s); using in-memory fallback.", exc)
            self._trip()
            return await getattr(self._fallback, method)(*args)
        if self._degraded:
            logger.info("Cache: Redis/Valkey backend recovered.")
            self._degraded = False
        return result

    # Raw bytes --------------------------------------------------------------------
    async def get(self, key: str) -> bytes | None:
        return await self._call("get", self._k(key))

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        if ttl > 0:
            await self._call("set", self._k(key), value, ttl)

    async def delete(self, key: str) -> None:
        await self._call("delete", self._k(key))

    async def incr(self, key: str, ttl: int) -> int:
        return int(await self._call("incr", self._k(key), ttl))

    async def reserve(self, key: str, interval: float, max_wait: float) -> float | None:
        return await self._call("reserve", self._k(key), interval, max_wait)

    async def ping(self) -> bool:
        try:
            return bool(await self.backend.ping())
        except Exception:
            return False

    async def close(self) -> None:
        try:
            await self.backend.close()
        except Exception:  # pragma: no cover - best effort
            pass

    # JSON / models -------------------------------------------------------------------
    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        await self.set(key, json.dumps(value, default=str).encode("utf-8"), ttl)

    async def get_model(self, key: str, model: type[M]) -> M | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return model.model_validate_json(raw)
        except ValueError:
            return None

    async def set_model(self, key: str, value: BaseModel, ttl: int) -> None:
        await self.set(key, value.model_dump_json().encode("utf-8"), ttl)


def make_key(namespace: str, payload: Any) -> str:
    """Deterministic cache key for a JSON-serializable payload."""
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return f"{namespace}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:40]}"
