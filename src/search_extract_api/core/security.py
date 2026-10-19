"""API-key authentication, request rate limiting and secret redaction for logs."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, Request, Response
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from ..errors import ApiError, ErrorCode

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API key")
_bearer = HTTPBearer(auto_error=False, description="API key as a Bearer token")


@dataclass(frozen=True)
class Principal:
    """The caller. ``owner_id`` is a one-way hash of the API key: sessions are owned by it."""

    name: str
    owner_id: str
    authenticated: bool

    @property
    def rate_key(self) -> str:
        return self.owner_id


ANONYMOUS_OWNER = "anonymous"


def owner_id_for_key(api_key: str) -> str:
    return "key_" + hashlib.sha256(f"search-extract-api:{api_key}".encode()).hexdigest()[:32]


def _match_key(candidate: str, keys: dict[str, str]) -> str | None:
    found: str | None = None
    for key, name in keys.items():
        # Compare against every key to keep timing independent of the match position.
        if secrets.compare_digest(candidate.encode(), key.encode()):
            found = name
    return found


async def get_principal(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    settings = request.app.state.settings
    keys = settings.api_key_map
    if not keys:
        client = request.client.host if request.client else "local"
        principal = Principal(name="anonymous", owner_id=ANONYMOUS_OWNER, authenticated=False)
        request.state.rate_identity = f"ip:{client}"
        request.state.principal = principal
        return principal
    candidate = api_key or (bearer.credentials if bearer else None)
    if not candidate:
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            "Missing API key (use the X-API-Key header or Authorization: Bearer).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    name = _match_key(candidate.strip(), keys)
    if name is None:
        raise ApiError(
            ErrorCode.UNAUTHORIZED, "Invalid API key.", headers={"WWW-Authenticate": "Bearer"}
        )
    principal = Principal(
        name=name, owner_id=owner_id_for_key(candidate.strip()), authenticated=True
    )
    request.state.rate_identity = principal.owner_id
    request.state.principal = principal
    return principal


async def enforce_rate_limit(
    request: Request, response: Response, principal: Principal = Depends(get_principal)
) -> Principal:
    settings = request.app.state.settings
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return principal
    cache = request.app.state.cache
    window = int(time.time() // 60)
    identity = getattr(request.state, "rate_identity", principal.owner_id)
    count = await cache.incr(f"rl:{identity}:{window}", 70)
    remaining = max(0, limit - count)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if count > limit:
        retry_after = max(1, 60 - int(time.time() % 60))
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            f"Rate limit of {limit} requests per minute exceeded.",
            details={"limit": limit, "retry_after": retry_after},
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(limit)},
        )
    return principal


# Dependency used by every protected router.
require_principal = enforce_rate_limit


class RedactingFilter(logging.Filter):
    """Masks secrets (cookies, tokens, API keys) that could end up in log lines."""

    _patterns = (
        (re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s'\"]+"), r"\1[REDACTED]"),
        (re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), r"\1[REDACTED]"),
        (re.compile(r"(?i)(cookie['\"]?\s*[:=]\s*['\"]?)[^'\"\n]+"), r"\1[REDACTED]"),
        (
            re.compile(r"(?i)([?&](?:token|api_key|apikey|key|access_token)=)[^&\s'\"]+"),
            r"\1[REDACTED]",
        ),
        (
            re.compile(r"(?i)\b(li_at|jsessionid|sessionid|sid|auth_token)=([^;\s'\"]+)"),
            r"\1=[REDACTED]",
        ),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        redacted = message
        for pattern, replacement in self._patterns:
            redacted = pattern.sub(replacement, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True
