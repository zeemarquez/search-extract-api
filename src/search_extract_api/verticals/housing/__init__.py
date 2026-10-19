"""Housing vertical: ``/housing/search`` and ``/housing/sources``."""

from __future__ import annotations

from ...adapters.base import SourceAdapter
from ...config import Settings
from ...schemas.common import BaseQuery, Result
from ...sessions.profiles import DomainProfile
from ..base import Vertical
from .schemas import HousingQuery, HousingResult
from .sources import (
    IDEALISTA_PROFILE,
    IdealistaApiAdapter,
    IdealistaWebAdapter,
    WebHousingAdapter,
    passes_filters,
)

__all__ = ["HousingQuery", "HousingResult", "HousingVertical"]


class HousingVertical(Vertical):
    name = "housing"
    title = "Housing"
    description = (
        "Real-estate listings (sale or rent) from Idealista — official API when keys are "
        "configured, website otherwise — and, on demand, other portals via web search."
    )
    prefix = "/housing"
    query_model = HousingQuery
    result_model = HousingResult

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [IdealistaApiAdapter(), IdealistaWebAdapter(), WebHousingAdapter()]

    def session_profiles(self) -> list[DomainProfile]:
        return [IDEALISTA_PROFILE]

    def postprocess(self, results: list[Result], query: BaseQuery) -> list[Result]:
        if not isinstance(query, HousingQuery):
            return results
        return [r for r in results if not isinstance(r, HousingResult) or passes_filters(r, query)]
