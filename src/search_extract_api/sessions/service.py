"""Session management: import, ownership, health checks and status tracking.

Ownership rule: a session belongs to the API key that created it (stored as a one-way
hash). Every lookup by another key answers ``404 SESSION_NOT_FOUND`` — the same as a
session that does not exist, so ids cannot be probed.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..config import Settings
from ..core.cookies import CookieFormat, StorageState, parse_cookies
from ..core.http import HttpService
from ..core.security import Principal
from ..core.urls import domain_matches, host_of
from ..errors import ApiError, ErrorCode
from .models import SessionCheckResult, SessionHandle, SessionInfo, SessionStatus
from .profiles import profile_for
from .store import SessionRow, SessionStore

if TYPE_CHECKING:
    from ..extraction.service import ExtractionService

logger = logging.getLogger(__name__)

MAX_COOKIES = 300


def _ts(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


def to_info(row: SessionRow) -> SessionInfo:
    return SessionInfo(
        id=row.id,
        domain=row.domain,
        label=row.label,
        source=row.source,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        status_reason=row.status_reason,
        cookie_count=row.cookie_count,
        local_storage_origins=row.origin_count,
        expires_at=_ts(row.expires_at),
        created_at=_ts(row.created_at),  # type: ignore[arg-type]
        updated_at=_ts(row.updated_at),  # type: ignore[arg-type]
        last_used_at=_ts(row.last_used_at),
        last_checked_at=_ts(row.last_checked_at),
    )


class SessionService:
    def __init__(self, *, settings: Settings, store: SessionStore, http: HttpService) -> None:
        self.settings = settings
        self.store = store
        self.http = http
        self.extraction: ExtractionService | None = None  # wired after construction

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _new_id() -> str:
        return "ses_" + secrets.token_urlsafe(18)

    def _prepare_state(self, domain: str, state: StorageState) -> StorageState:
        filtered = state.filter_domain(domain)
        if not filtered.cookies:
            raise ApiError(
                ErrorCode.SESSION_INVALID,
                f"None of the cookies belong to {domain}.",
                details={"cookies_received": len(state.cookies)},
            )
        if len(filtered.cookies) > MAX_COOKIES:
            raise ApiError(ErrorCode.SESSION_INVALID, f"Too many cookies (max {MAX_COOKIES}).")
        return filtered

    @staticmethod
    def _expiry(domain: str, state: StorageState) -> float | None:
        profile = profile_for(domain)
        return state.earliest_expiry(profile.auth_cookies) if profile.auth_cookies else None

    async def _owned_row(self, principal: Principal, session_id: str) -> SessionRow:
        row = await self.store.get_row(session_id)
        if row is None or not secrets.compare_digest(row.owner_id, principal.owner_id):
            raise ApiError(
                ErrorCode.SESSION_NOT_FOUND,
                "Session not found.",
                details={"session_id": session_id},
            )
        return row

    # ------------------------------------------------------------------ CRUD
    async def create(
        self,
        principal: Principal,
        domain: str,
        state: StorageState,
        *,
        source: str,
        label: str | None = None,
    ) -> SessionInfo:
        state = self._prepare_state(domain, state)
        now = time.time()
        row = SessionRow(
            id=self._new_id(),
            owner_id=principal.owner_id,
            domain=domain,
            label=label,
            source=source,
            status="unknown",
            status_reason=None,
            cookie_count=len(state.cookies),
            origin_count=len(state.origins),
            expires_at=self._expiry(domain, state),
            created_at=now,
            updated_at=now,
            last_used_at=None,
            last_checked_at=None,
        )
        await self.store.insert(row, state.model_dump_json().encode("utf-8"))
        logger.info(
            "Session %s created for %s (%s, %d cookies)", row.id, domain, source, row.cookie_count
        )
        return to_info(row)

    async def import_cookies(
        self,
        principal: Principal,
        domain: str,
        cookies: object,
        fmt: CookieFormat = "auto",
        label: str | None = None,
    ) -> SessionInfo:
        state = parse_cookies(cookies, fmt)
        return await self.create(principal, domain, state, source="import", label=label)

    async def replace_state(
        self,
        principal: Principal,
        session_id: str,
        *,
        state: StorageState | None = None,
        label: str | None = None,
    ) -> SessionInfo:
        row = await self._owned_row(principal, session_id)
        fields: dict[str, object] = {}
        if state is not None:
            state = self._prepare_state(row.domain, state)
            fields.update(
                data=state.model_dump_json().encode("utf-8"),
                cookie_count=len(state.cookies),
                origin_count=len(state.origins),
                expires_at=self._expiry(row.domain, state),
                status="unknown",
                status_reason=None,
            )
        if label is not None:
            fields["label"] = label
            fields.setdefault("updated_at", time.time())
        await self.store.update(session_id, **fields)
        return to_info(await self._owned_row(principal, session_id))

    async def list(self, principal: Principal) -> list[SessionInfo]:
        return [to_info(row) for row in await self.store.list(principal.owner_id)]

    async def get(self, principal: Principal, session_id: str) -> SessionInfo:
        return to_info(await self._owned_row(principal, session_id))

    async def delete(self, principal: Principal, session_id: str) -> None:
        await self._owned_row(principal, session_id)
        await self.store.delete(session_id)
        logger.info("Session %s deleted", session_id)

    # ------------------------------------------------------------------ usage
    async def resolve(
        self, principal: Principal, session_id: str, *, url: str | None = None
    ) -> SessionHandle:
        """Loads a session for use. Fails with the session's own error codes."""
        row = await self._owned_row(principal, session_id)
        if url is not None and not domain_matches(host_of(url), row.domain):
            raise ApiError(
                ErrorCode.SESSION_DOMAIN_MISMATCH,
                f"Session {session_id} belongs to {row.domain}, not {host_of(url)}.",
                details={"session_id": session_id, "session_domain": row.domain},
            )
        found = await self.store.get(session_id)
        if found is None:  # deleted concurrently
            raise ApiError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
        _, payload = found
        state = StorageState.model_validate(json.loads(payload))
        profile = profile_for(row.domain)
        if profile.auth_cookies:
            now = time.time()
            live = {c.name for c in state.cookies if not c.is_expired(now)}
            if not any(name in live for name in profile.auth_cookies):
                await self.record_failure(
                    session_id, ErrorCode.SESSION_EXPIRED, "auth cookies expired"
                )
                raise ApiError(
                    ErrorCode.SESSION_EXPIRED,
                    "The session's login cookies have expired. Import fresh cookies or log in again.",
                    details={"session_id": session_id, "domain": row.domain},
                )
        return SessionHandle(id=row.id, domain=row.domain, owner_id=row.owner_id, state=state)

    async def record_success(self, session_id: str) -> None:
        await self.store.update(
            session_id, status="active", status_reason=None, last_used_at=time.time()
        )

    async def record_failure(self, session_id: str, code: ErrorCode | str, reason: str) -> None:
        status: SessionStatus = "captcha" if str(code) == ErrorCode.CAPTCHA_REQUIRED else "expired"
        await self.store.update(session_id, status=status, status_reason=reason[:300])

    # ------------------------------------------------------------------ health
    async def check(self, principal: Principal, session_id: str) -> SessionCheckResult:
        """Verifies a session against the site: active, expired or captcha."""
        row = await self._owned_row(principal, session_id)
        profile = profile_for(row.domain)
        check_url = profile.check_url or f"https://{row.domain}/"
        status: SessionStatus = "active"
        reason: str | None = None
        http_status: int | None = None
        try:
            handle = await self.resolve(principal, session_id)
            if self.extraction is None:
                raise ApiError(ErrorCode.INTERNAL_ERROR, "Extraction service not wired.")
            page = await self.extraction.fetch_page(check_url, session=handle, mode="auto")
            http_status = page.status_code
        except ApiError as exc:
            if exc.code == ErrorCode.SESSION_EXPIRED:
                status, reason = "expired", str(exc.details.get("reason") or exc.message)
            elif exc.code == ErrorCode.CAPTCHA_REQUIRED:
                status, reason = "captcha", str(exc.details.get("reason") or exc.message)
            elif exc.code in (ErrorCode.SESSION_RATE_LIMITED, ErrorCode.DOMAIN_RATE_LIMITED):
                raise
            else:
                status, reason = "error", exc.message
                http_status = exc.details.get("status_code") if exc.details else None
        now = time.time()
        await self.store.update(
            session_id, status=status, status_reason=reason, last_checked_at=now
        )
        info = to_info(await self._owned_row(principal, session_id))
        return SessionCheckResult(
            session=info,
            status=status,
            reason=reason,
            checked_url=check_url,
            http_status=http_status,
        )
