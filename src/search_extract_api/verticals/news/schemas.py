from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from ...core.urls import normalize_domain
from ...schemas.common import BaseQuery, Result, _split_csv_list


class NewsResult(Result):
    """News article. ``published_at`` (inherited) is the publication date."""

    outlet: str | None = Field(default=None, description="Media outlet (e.g. 'El País').")
    author: str | None = None


class NewsQuery(BaseQuery):
    q: str = Field(min_length=1, max_length=500, description="Query text.")
    domains: list[str] | None = Field(
        default=None, description="Only these outlet domains (comma-separated)."
    )
    exclude_domains: list[str] | None = Field(
        default=None, description="Exclude these outlet domains (comma-separated)."
    )
    resolve_links: bool = Field(
        default=False,
        description="Resolve Google News redirect links to publisher URLs (two extra "
        "requests to Google per link, cached).",
    )

    @field_validator("domains", "exclude_domains", mode="before")
    @classmethod
    def _domains(cls, value: Any) -> Any:
        items = _split_csv_list(value)
        if not items:
            return None
        return [normalize_domain(item) for item in items if normalize_domain(item)]


NewsTopic = Literal[
    "world", "nation", "business", "technology", "entertainment", "sports", "science", "health"
]
