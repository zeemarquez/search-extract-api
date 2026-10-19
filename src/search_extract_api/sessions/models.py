"""Session schemas. Session contents (cookies, local storage) never leave the service:
only :class:`SessionInfo` metadata is ever returned to clients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..core.cookies import CookieFormat, StorageState
from ..core.urls import normalize_domain

SessionStatus = Literal["unknown", "active", "expired", "captcha", "error"]
SessionSource = Literal["import", "login"]


@dataclass(frozen=True)
class SessionHandle:
    """A decrypted session, only ever held in memory while serving a request."""

    id: str
    domain: str
    owner_id: str
    state: StorageState

    def __repr__(self) -> str:  # never print cookie values
        return f"SessionHandle(id={self.id!r}, domain={self.domain!r})"


class SessionInfo(BaseModel):
    id: str
    domain: str
    label: str | None = None
    source: SessionSource
    status: SessionStatus
    status_reason: str | None = None
    cookie_count: int
    local_storage_origins: int = 0
    expires_at: datetime | None = Field(
        default=None, description="Earliest expiry among the session's auth cookies."
    )
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    last_checked_at: datetime | None = None


class SessionImportRequest(BaseModel):
    domain: str = Field(description="Site the session belongs to, e.g. 'linkedin.com'.")
    cookies: str | list[dict[str, Any]] | dict[str, Any] = Field(
        description="Cookies exported from the browser: JSON array (Cookie-Editor, "
        "EditThisCookie...), Playwright storage state object, or cookies.txt text."
    )
    format: CookieFormat = "auto"
    label: str | None = Field(default=None, max_length=120)
    check: bool = Field(default=False, description="Run a health check right after import.")

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        domain = normalize_domain(value)
        if "." not in domain or len(domain) > 253:
            raise ValueError("domain must look like 'example.com'")
        return domain


class SessionUpdateRequest(BaseModel):
    cookies: str | list[dict[str, Any]] | dict[str, Any] | None = None
    format: CookieFormat = "auto"
    label: str | None = Field(default=None, max_length=120)


class SessionCheckResult(BaseModel):
    session: SessionInfo
    status: SessionStatus
    reason: str | None = None
    checked_url: str | None = None
    http_status: int | None = None


class LoginStartRequest(BaseModel):
    domain: str = Field(description="Site to log into, e.g. 'linkedin.com'.")
    login_url: str | None = Field(default=None, description="Defaults to the site's login page.")
    label: str | None = Field(default=None, max_length=120)
    session_id: str | None = Field(
        default=None, description="Existing session to refresh instead of creating a new one."
    )

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        domain = normalize_domain(value)
        if "." not in domain:
            raise ValueError("domain must look like 'example.com'")
        return domain


LoginStatus = Literal["pending", "completed", "cancelled", "expired", "failed"]


class LoginInfo(BaseModel):
    login_id: str
    domain: str
    status: LoginStatus
    viewer_url: str | None = Field(
        default=None,
        description="Open this URL in a browser to see and drive the remote browser. "
        "It contains a one-time secret in the fragment; share it only with the user.",
    )
    expires_at: datetime
    session: SessionInfo | None = None
    error: str | None = None
