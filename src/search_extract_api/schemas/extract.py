"""Schemas for ``/extract`` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.lang import normalize_language
from ..extraction.fingerprint import Fingerprint

ExtractMode = Literal["auto", "crawl4ai", "light", "browser"]
ContentScope = Literal["main", "full"]


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Page to extract (http/https).")
    mode: ExtractMode | None = Field(
        default=None,
        description="Fetcher: auto (Crawl4AI, then lightweight fallback; headless browser "
        "when a session is used), crawl4ai, light or browser.",
    )
    session_id: str | None = Field(default=None, description="Use an authenticated session.")
    content: ContentScope = Field(
        default="main", description="main = article/main content only; full = whole page."
    )
    max_chars: int | None = Field(default=None, ge=200, le=2_000_000)
    lang: str | None = Field(default=None, description="Preferred language (Accept-Language).")
    previous_fingerprint: str | None = Field(
        default=None, description="Fingerprint hash from a previous extraction, to get `changed`."
    )
    previous_simhash: str | None = Field(
        default=None, description="Previous SimHash, to get `similarity` (0..1)."
    )
    include_links: bool = False
    no_cache: bool = False

    @field_validator("lang")
    @classmethod
    def _lang(cls, value: str | None) -> str | None:
        return normalize_language(value) if value else None

    @field_validator("previous_simhash")
    @classmethod
    def _simhash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower().removeprefix("0x")
        int(value, 16)  # raises ValueError -> 422 if not hex
        return value


class Link(BaseModel):
    url: str
    text: str = ""


class ExtractResponse(BaseModel):
    url: str = Field(description="Final URL after redirects (canonicalized).")
    requested_url: str
    canonical_url: str | None = Field(default=None, description="<link rel=canonical> if any.")
    status_code: int
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    language: str | None = None
    image: str | None = None
    site_name: str | None = None
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    content: ContentScope = "main"
    markdown: str = Field(description="Clean text in Markdown, ready for an LLM.")
    word_count: int
    truncated: bool = False
    fingerprint: Fingerprint
    changed: bool | None = Field(default=None, description="Only if previous_fingerprint was sent.")
    similarity: float | None = Field(default=None, description="Only if previous_simhash was sent.")
    links: list[Link] | None = None
    fetcher: str = Field(description="crawl4ai | browser | light")
    extractor: str = Field(description="trafilatura | crawl4ai | markdownify | fallback")
    session_id: str | None = None
    fetched_at: datetime
    cached: bool = False
    elapsed_ms: int = 0


class BatchExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(min_length=1, max_length=50)
    mode: ExtractMode | None = None
    session_id: str | None = None
    content: ContentScope = "main"
    max_chars: int | None = Field(default=None, ge=200, le=2_000_000)
    lang: str | None = None
    no_cache: bool = False
    concurrency: int = Field(default=4, ge=1, le=10)


class BatchItem(BaseModel):
    url: str
    ok: bool
    result: ExtractResponse | None = None
    error: dict[str, Any] | None = None


class BatchExtractResponse(BaseModel):
    items: list[BatchItem]
    succeeded: int
    failed: int


FieldType = Literal["string", "number", "integer", "boolean", "date", "array", "object"]


class FieldSpec(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    description: str | None = Field(default=None, max_length=500)
    type: FieldType = "string"
    required: bool = False


class StructuredRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    text: str | None = Field(default=None, max_length=500_000)
    fields: list[FieldSpec] | None = Field(default=None, max_length=60)
    json_schema: dict[str, Any] | None = Field(
        default=None, description="Alternative to `fields`: a JSON Schema of the object."
    )
    instructions: str | None = Field(default=None, max_length=2000)
    session_id: str | None = None
    mode: ExtractMode | None = None
    lang: str | None = None

    @model_validator(mode="after")
    def _check(self) -> StructuredRequest:
        if bool(self.url) == bool(self.text):
            raise ValueError("provide exactly one of 'url' or 'text'")
        if bool(self.fields) == bool(self.json_schema):
            raise ValueError("provide exactly one of 'fields' or 'json_schema'")
        return self


class StructuredResponse(BaseModel):
    data: dict[str, Any]
    missing_fields: list[str] = Field(default_factory=list)
    url: str | None = None
    title: str | None = None
    model: str
    source_chars: int
    truncated: bool = False
    fetcher: str | None = None
