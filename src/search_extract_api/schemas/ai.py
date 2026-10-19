"""Schemas for the optional AI endpoints (``/ai/*``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .extract import ExtractMode, FieldSpec


class TextItem(BaseModel):
    """A text or a search result. ``snippet`` and ``content`` are accepted as aliases
    of ``text`` so results from ``/search`` can be passed as-is."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    title: str | None = Field(default=None, max_length=2000)
    text: str | None = Field(default=None, max_length=200_000)
    url: str | None = None
    source: str | None = None
    published_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("text"):
            data = dict(data)
            data["text"] = data.get("snippet") or data.get("content") or data.get("markdown")
        if isinstance(data, dict) and data.get("published_at") is not None:
            data["published_at"] = str(data["published_at"])
        return data

    def as_text(self, max_chars: int | None = None) -> str:
        body = self.text or ""
        if max_chars is not None and len(body) > max_chars:
            body = body[:max_chars].rsplit(" ", 1)[0] + "…"
        return "\n".join(part for part in (self.title, body) if part)


SummaryLength = Literal["short", "medium", "long"]


class SummarizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=2_000_000)
    items: list[TextItem] | None = Field(default=None, max_length=300)
    language: str | None = Field(
        default=None, description="Language of the summary (ISO 639-1). Default: the input's."
    )
    length: SummaryLength = "medium"
    max_words: int | None = Field(default=None, ge=10, le=3000)
    format: Literal["paragraph", "bullets"] = "paragraph"
    focus: str | None = Field(default=None, max_length=1000, description="What to focus on.")

    @model_validator(mode="after")
    def _one_input(self) -> SummarizeRequest:
        if bool(self.text) == bool(self.items):
            raise ValueError("provide exactly one of 'text' or 'items'")
        return self


class SummarizeResponse(BaseModel):
    summary: str
    language: str | None = None
    format: str
    target_words: int
    model: str
    input_items: int
    input_chars: int
    chunks: int = 1
    truncated: bool = False


class DedupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TextItem] = Field(min_length=1, max_length=1000)
    threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Similarity needed to group two items (default depends on the method).",
    )
    method: Literal["auto", "embeddings", "lexical"] = Field(
        default="auto", description="auto = embeddings if configured, otherwise lexical."
    )


class DedupCluster(BaseModel):
    id: str
    representative: str
    items: list[str]
    size: int


class DedupResponse(BaseModel):
    clusters: list[DedupCluster]
    duplicates: int = Field(description="Items that were grouped under another item.")
    method: Literal["embeddings", "lexical"]
    threshold: float
    model: str | None = None


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=500_000)
    items: list[TextItem] | None = Field(default=None, max_length=200)
    labels: list[str] | None = Field(
        default=None, max_length=200, description="Allowed labels. Omit for free labels."
    )
    multi_label: bool = False
    max_labels: int = Field(default=3, ge=1, le=20)
    instructions: str | None = Field(default=None, max_length=2000)
    language: str | None = Field(default=None, description="Language for free labels.")

    @model_validator(mode="after")
    def _one_input(self) -> ClassifyRequest:
        if bool(self.text) == bool(self.items):
            raise ValueError("provide exactly one of 'text' or 'items'")
        if self.labels is not None:
            self.labels = [label.strip() for label in self.labels if label and label.strip()]
            if not self.labels:
                raise ValueError("'labels' must contain at least one non-empty label")
        return self


class LabelScore(BaseModel):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


class ClassifyResult(BaseModel):
    id: str | None = None
    labels: list[LabelScore]


class ClassifyResponse(BaseModel):
    results: list[ClassifyResult]
    mode: Literal["labels", "free"]
    model: str


class VerifyRequest(BaseModel):
    """Verification mode of classification: does this text meet this criterion?"""

    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(min_length=3, max_length=2000, examples=["Announces a strike in Madrid"])
    text: str | None = Field(default=None, max_length=500_000)
    url: str | None = None
    session_id: str | None = None
    mode: ExtractMode | None = None
    fields: list[FieldSpec] | None = Field(
        default=None, max_length=40, description="Fields to extract from the text."
    )
    language: str | None = Field(default=None, description="Language for the 'reason'.")

    @model_validator(mode="after")
    def _one_input(self) -> VerifyRequest:
        if bool(self.text) == bool(self.url):
            raise ValueError("provide exactly one of 'text' or 'url'")
        return self


class VerifyResponse(BaseModel):
    matches: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    fields: dict[str, Any] = Field(default_factory=dict)
    model: str
    url: str | None = None
    source_chars: int
    truncated: bool = False
