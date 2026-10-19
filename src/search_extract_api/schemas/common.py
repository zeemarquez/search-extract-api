"""Common schemas shared by the core and every vertical.

``Result`` is the common result schema. Verticals subclass it to add their own
fields (``NewsResult``, ``JobResult``, ``HousingResult``...). ``BaseQuery`` carries the
common filters (language, region, dates) and verticals subclass it the same way.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.dates import ensure_utc
from ..core.lang import normalize_language
from ..core.urls import canonicalize_url, result_id

TimeRange = Literal["day", "week", "month", "year"]
SortOrder = Literal["relevance", "date"]
FallbackMode = Literal["auto", "always", "never"]


class ErrorInfo(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """Shape of every error response."""

    class Body(BaseModel):
        code: str = Field(examples=["SESSION_EXPIRED"])
        message: str
        details: dict[str, Any] = Field(default_factory=dict)

    error: Body


class Result(BaseModel):
    """Common result schema returned by every search, feed and vertical endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        default="",
        description="Stable id derived from the canonical URL (equal for URL variants).",
    )
    url: str = Field(description="Canonical URL (tracking parameters removed).")
    title: str | None = None
    source: str | None = Field(default=None, description="Site that published it, e.g. elpais.com")
    published_at: datetime | None = Field(default=None, description="Publication date (UTC).")
    language: str | None = Field(default=None, description="ISO 639-1 code.")
    snippet: str | None = None
    image: str | None = None
    providers: list[str] = Field(
        default_factory=list, description="Providers/sources that found this result."
    )
    score: float | None = Field(default=None, description="Fused relevance score.")
    extra: dict[str, Any] = Field(default_factory=dict, description="Provider-specific data.")

    @model_validator(mode="before")
    @classmethod
    def _canonical(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("url"):
            data = dict(data)
            data["url"] = canonicalize_url(str(data["url"]))
            if not data.get("id"):
                data["id"] = result_id(data["url"])
        return data

    @field_validator("published_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)

    @field_validator("language")
    @classmethod
    def _lang(cls, value: str | None) -> str | None:
        return normalize_language(value)


class SourceStatus(BaseModel):
    """Outcome of one source (adapter) for a request."""

    name: str
    ok: bool
    skipped: bool = Field(
        default=False,
        description="Not queried (e.g. not configured, or needs a session that was not "
        "given). Skipped sources do not count as failures.",
    )
    count: int = 0
    elapsed_ms: int = 0
    fallback: bool = False
    error: ErrorInfo | None = None
    details: dict[str, Any] = Field(default_factory=dict)


R = TypeVar("R", bound=Result)


class SearchResponse(BaseModel, Generic[R]):
    """Envelope for any multi-source search. Never fails because of a single source:
    failed sources are listed in ``failed_sources`` and ``sources[].error``."""

    query: dict[str, Any]
    results: list[R]
    count: int
    sources: list[SourceStatus]
    failed_sources: list[str] = Field(default_factory=list)
    partial: bool = Field(default=False, description="True if at least one source failed.")
    cached: bool = False
    took_ms: int = 0


def _split_csv_list(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list | tuple):
        out: list[str] = []
        for item in value:
            for part in str(item).split(","):
                part = part.strip()
                if part:
                    out.append(part)
        return out or None
    return value


class BaseQuery(BaseModel):
    """Common filters. Used both as query parameters (GET) and JSON body (POST)."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, max_length=500, description="Query text.")
    lang: str | None = Field(default=None, description="Language, ISO 639-1 (e.g. 'es').")
    region: str | None = Field(default=None, description="Region, ISO 3166-1 alpha-2 (e.g. 'ES').")
    time_range: TimeRange | None = None
    since: datetime | None = Field(default=None, description="Only results published after.")
    until: datetime | None = Field(default=None, description="Only results published before.")
    limit: int = Field(default=20, ge=1, le=100)
    page: int = Field(default=1, ge=1, le=10)
    sort: SortOrder = "relevance"
    sources: list[str] | None = Field(
        default=None, description="Restrict to these sources (comma-separated)."
    )
    fallback: FallbackMode | None = Field(
        default=None,
        description="Use commercial fallback providers: auto (if others fail or return "
        "few results), always, never. Default: server setting.",
    )
    session_id: str | None = Field(
        default=None, description="Authenticated session for sources that need one."
    )
    no_cache: bool = False

    @field_validator("sources", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> Any:
        return _split_csv_list(value)

    @field_validator("lang")
    @classmethod
    def _lang(cls, value: str | None) -> str | None:
        return normalize_language(value) if value else None

    @field_validator("region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("region must be an ISO 3166-1 alpha-2 code, e.g. 'ES'")
        return value

    @field_validator("since", "until")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)

    @field_validator("q")
    @classmethod
    def _strip_q(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    def cache_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"no_cache"})


class WebQuery(BaseQuery):
    q: str = Field(min_length=1, max_length=500, description="Query text.")
    safesearch: Literal[0, 1, 2] = Field(default=1, description="0 off, 1 moderate, 2 strict.")
