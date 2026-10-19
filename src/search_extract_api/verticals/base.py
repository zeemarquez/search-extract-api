"""Vertical interface and registry.

A *vertical* is a module that specializes the generic machinery for one domain
(news, jobs, housing...): its own endpoint prefix, query filters, extended result
schema and set of source adapters. Verticals are discovered from the built-in list
and from the ``search_extract_api.verticals`` entry point group, so new ones can be
added from separate packages without touching the core.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable
from importlib.metadata import entry_points
from typing import Any, ClassVar

from fastapi import APIRouter

from ..adapters.base import SourceAdapter
from ..config import Settings
from ..extraction.resolvers import UrlResolver
from ..schemas.common import BaseQuery, Result
from ..sessions.profiles import DomainProfile

logger = logging.getLogger(__name__)

VERTICAL_ENTRY_POINT = "search_extract_api.verticals"
SOURCE_ENTRY_POINT = "search_extract_api.sources"

BUILTIN_VERTICALS = (
    "search_extract_api.verticals.web:WebVertical",
    "search_extract_api.verticals.news:NewsVertical",
    "search_extract_api.verticals.providers:LinkedInVertical",
    "search_extract_api.verticals.providers:IdealistaVertical",
    "search_extract_api.verticals.providers:RemotiveVertical",
    "search_extract_api.verticals.providers:ArbeitnowVertical",
)


class Vertical:
    """Base class for verticals."""

    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: URL prefix, e.g. ``/news``. The web vertical uses ``""`` (served at ``/search``).
    prefix: ClassVar[str] = ""
    query_model: ClassVar[type[BaseQuery]] = BaseQuery
    result_model: ClassVar[type[Result]] = Result
    #: Merge results with the same headline on the same site (catches redirect URLs).
    title_merge: ClassVar[bool] = True
    #: Path of the search endpoint under ``prefix`` (e.g. ``/linkedin`` + ``/jobs``).
    search_path: ClassVar[str] = "/search"

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        """Built-in source adapters, in priority order."""
        return []

    def session_profiles(self) -> list[DomainProfile]:
        """Login/captcha detection profiles for the sites this vertical logs into."""
        return []

    def url_resolvers(self) -> list[UrlResolver]:
        """Resolvers for redirector links this vertical produces (used by extraction)."""
        return []

    def postprocess(self, results: list[Result], query: BaseQuery) -> list[Result]:
        """Vertical-specific filtering/normalization after merging."""
        return results

    def extra_routes(self, router: APIRouter) -> None:
        """Hook to add endpoints beyond ``/search`` and ``/sources``."""

    @property
    def sources_path(self) -> str:
        return "/sources" if self.prefix else "/search/sources"

    def describe(self, sources: Iterable[SourceAdapter], settings: Settings) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "search_endpoint": f"{self.prefix}{self.search_path}",
            "query_schema": self.query_model.model_json_schema(),
            "result_schema": self.result_model.model_json_schema(),
            "sources": [s.info(settings).model_dump() for s in sources],
        }


def _load_object(path: str) -> Any:
    module_name, _, attr = path.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


class VerticalRegistry:
    """Holds the enabled verticals and their sources (built-in + plugins)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.verticals: dict[str, Vertical] = {}
        self.sources: dict[str, list[SourceAdapter]] = {}

    def register(self, vertical: Vertical) -> None:
        if not vertical.name:
            raise ValueError("vertical.name is required")
        if not issubclass(vertical.result_model, Result):
            raise TypeError(f"{vertical.name}: result_model must subclass Result")
        if not issubclass(vertical.query_model, BaseQuery):
            raise TypeError(f"{vertical.name}: query_model must subclass BaseQuery")
        self.verticals[vertical.name] = vertical
        disabled = self.settings.disabled_source_list
        adapters = [a for a in vertical.build_sources(self.settings) if a.name not in disabled]
        self.sources[vertical.name] = adapters

    def add_source(self, vertical_name: str, adapter: SourceAdapter) -> None:
        if vertical_name not in self.verticals:
            logger.warning("Source %s targets unknown vertical %s", adapter.name, vertical_name)
            return
        vertical = self.verticals[vertical_name]
        if not issubclass(adapter.result_model, vertical.result_model):
            raise TypeError(
                f"Source {adapter.name} produces {adapter.result_model.__name__}, "
                f"but vertical {vertical_name} expects {vertical.result_model.__name__}"
            )
        if adapter.name in self.settings.disabled_source_list:
            return
        existing = {a.name for a in self.sources[vertical_name]}
        if adapter.name in existing:
            raise ValueError(f"Duplicate source name {adapter.name} in {vertical_name}")
        self.sources[vertical_name].append(adapter)

    def get(self, name: str) -> Vertical:
        return self.verticals[name]

    def sources_for(self, name: str) -> list[SourceAdapter]:
        return list(self.sources.get(name, []))

    def all_resolvers(self) -> list[UrlResolver]:
        resolvers: list[UrlResolver] = []
        for vertical in self.verticals.values():
            resolvers.extend(vertical.url_resolvers())
        return resolvers

    def all_profiles(self) -> list[DomainProfile]:
        profiles: list[DomainProfile] = []
        for vertical in self.verticals.values():
            profiles.extend(vertical.session_profiles())
        return profiles

    @classmethod
    def load(cls, settings: Settings, extra: Iterable[Vertical] = ()) -> VerticalRegistry:
        registry = cls(settings)
        enabled = set(settings.enabled_vertical_list)
        candidates: list[Vertical] = []
        for path in BUILTIN_VERTICALS:
            candidates.append(_load_object(path)())
        for ep in entry_points(group=VERTICAL_ENTRY_POINT):
            try:
                candidates.append(ep.load()())
            except Exception:
                logger.exception("Could not load vertical plugin %s", ep.name)
        candidates.extend(extra)
        for vertical in candidates:
            # The web vertical is part of the core and always enabled.
            if enabled and vertical.name not in enabled and vertical.name != "web":
                continue
            registry.register(vertical)
        for ep in entry_points(group=SOURCE_ENTRY_POINT):
            try:
                factory: Callable[[Settings], SourceAdapter] = ep.load()
                adapter = factory(settings)
                registry.add_source(getattr(adapter, "vertical", "web"), adapter)
            except Exception:
                logger.exception("Could not load source plugin %s", ep.name)
        return registry
