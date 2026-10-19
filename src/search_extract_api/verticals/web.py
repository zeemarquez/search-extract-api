"""The general web search, served at ``/search``. Part of the core."""

from __future__ import annotations

from ..adapters.base import SourceAdapter
from ..adapters.commercial import ExaAdapter, TavilyAdapter
from ..adapters.searxng import SearxngAdapter
from ..config import Settings
from ..schemas.common import Result, WebQuery
from .base import Vertical


class WebVertical(Vertical):
    name = "web"
    title = "Web search"
    description = "General web search across SearXNG engines, with optional commercial fallbacks."
    prefix = ""
    query_model = WebQuery
    result_model = Result
    # Different pages of a site can share a title; only merge by URL here.
    title_merge = False

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [SearxngAdapter(), TavilyAdapter(), ExaAdapter()]
