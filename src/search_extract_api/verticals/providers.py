"""Per-provider endpoints: ``/linkedin/jobs``, ``/idealista/listings``, ``/remotive/jobs``
and ``/arbeitnow/jobs``.

Each provider is its own vertical, so its query parameters and result fields are the
provider's own and a new provider never changes another's endpoint.
"""

from __future__ import annotations

from pydantic import Field

from ..adapters.base import SourceAdapter
from ..config import Settings
from ..schemas.common import BaseQuery, Result
from ..sessions.profiles import DomainProfile
from .base import Vertical
from .housing.schemas import HousingQuery, HousingResult
from .housing.sources import (
    IDEALISTA_PROFILE,
    IdealistaApiAdapter,
    IdealistaWebAdapter,
    passes_filters,
)
from .jobs.schemas import JobResult, JobsQuery
from .jobs.sources import (
    LINKEDIN_PROFILE,
    ArbeitnowAdapter,
    LinkedInJobsAdapter,
    RemotiveAdapter,
    guess_work_mode,
)


class LinkedInJobsQuery(JobsQuery):
    session_id: str = Field(  # type: ignore[assignment]
        description="Your LinkedIn session (POST /sessions or /sessions/login)."
    )


def _normalize_jobs(results: list[Result], query: BaseQuery) -> list[Result]:
    wanted = getattr(query, "work_mode", None)
    kept: list[Result] = []
    for result in results:
        if isinstance(result, JobResult):
            if result.work_mode is None:
                result.work_mode = guess_work_mode(result.location, result.job_title)
            if not result.job_title:
                result.job_title = result.title
            # Jobs whose mode is unknown are kept: the provider did not say.
            if wanted and result.work_mode not in (None, wanted):
                continue
        kept.append(result)
    return kept


class _JobsProvider(Vertical):
    query_model = JobsQuery
    result_model = JobResult
    search_path = "/jobs"

    def postprocess(self, results: list[Result], query: BaseQuery) -> list[Result]:
        return _normalize_jobs(results, query)


class LinkedInVertical(_JobsProvider):
    name = "linkedin"
    title = "LinkedIn"
    description = (
        "LinkedIn job search with your own LinkedIn session (required). Automated access "
        "may break LinkedIn's terms of service: use at your own risk."
    )
    prefix = "/linkedin"
    query_model = LinkedInJobsQuery

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [LinkedInJobsAdapter()]

    def session_profiles(self) -> list[DomainProfile]:
        return [LINKEDIN_PROFILE]


class RemotiveVertical(_JobsProvider):
    name = "remotive"
    title = "Remotive"
    description = "Remote jobs from remotive.com (results link to Remotive, as its terms require)."
    prefix = "/remotive"

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [RemotiveAdapter()]


class ArbeitnowVertical(_JobsProvider):
    name = "arbeitnow"
    title = "Arbeitnow"
    description = "Jobs in Europe (mostly Germany) from the Arbeitnow job board."
    prefix = "/arbeitnow"

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [ArbeitnowAdapter()]


class IdealistaVertical(Vertical):
    name = "idealista"
    title = "Idealista"
    description = (
        "Idealista listings (sale or rent): the official API when IDEALISTA_API_KEY/SECRET "
        "are configured, the website otherwise (a session reduces captchas)."
    )
    prefix = "/idealista"
    search_path = "/listings"
    query_model = HousingQuery
    result_model = HousingResult

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [IdealistaApiAdapter(), IdealistaWebAdapter()]

    def session_profiles(self) -> list[DomainProfile]:
        return [IDEALISTA_PROFILE]

    def postprocess(self, results: list[Result], query: BaseQuery) -> list[Result]:
        if not isinstance(query, HousingQuery):
            return results
        return [r for r in results if not isinstance(r, HousingResult) or passes_filters(r, query)]
