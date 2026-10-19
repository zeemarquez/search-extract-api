"""Jobs vertical: ``/jobs/search`` and ``/jobs/sources``."""

from __future__ import annotations

from ...adapters.base import SourceAdapter
from ...config import Settings
from ...schemas.common import BaseQuery, Result
from ...sessions.profiles import DomainProfile
from ..base import Vertical
from .schemas import JobResult, JobsQuery
from .sources import (
    LINKEDIN_PROFILE,
    ArbeitnowAdapter,
    LinkedInJobsAdapter,
    RemotiveAdapter,
    WebJobsAdapter,
    guess_work_mode,
)

__all__ = ["JobResult", "JobsQuery", "JobsVertical"]


class JobsVertical(Vertical):
    name = "jobs"
    title = "Jobs"
    description = (
        "Job search across LinkedIn (with your session), Remotive, Arbeitnow and, on demand, "
        "job boards found through web search."
    )
    prefix = "/jobs"
    query_model = JobsQuery
    result_model = JobResult

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [LinkedInJobsAdapter(), RemotiveAdapter(), ArbeitnowAdapter(), WebJobsAdapter()]

    def session_profiles(self) -> list[DomainProfile]:
        return [LINKEDIN_PROFILE]

    def postprocess(self, results: list[Result], query: BaseQuery) -> list[Result]:
        wanted = getattr(query, "work_mode", None)
        kept: list[Result] = []
        for result in results:
            if isinstance(result, JobResult):
                if result.work_mode is None:
                    result.work_mode = guess_work_mode(result.location, result.job_title)
                if not result.job_title:
                    result.job_title = result.title
                if wanted and result.work_mode not in (None, wanted):
                    continue
            kept.append(result)
        return kept
