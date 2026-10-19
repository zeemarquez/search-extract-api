from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ...schemas.common import BaseQuery, Result

WorkMode = Literal["onsite", "remote", "hybrid"]


class JobResult(Result):
    """Job posting. ``published_at`` (inherited) is the posting date."""

    job_title: str | None = Field(default=None, description="Position (puesto).")
    company: str | None = None
    location: str | None = None
    work_mode: WorkMode | None = Field(default=None, description="onsite | remote | hybrid")
    employment_type: str | None = Field(default=None, description="full_time, contract...")
    salary: str | None = None


class JobsQuery(BaseQuery):
    q: str | None = Field(default=None, max_length=300, description="Keywords (title, skills).")
    location: str | None = Field(
        default=None, max_length=200, description="City, region or country."
    )
    work_mode: WorkMode | None = None
    company: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _need_something(self) -> JobsQuery:
        if not self.q and not self.location and not self.company:
            raise ValueError("provide at least one of 'q', 'location' or 'company'")
        return self
