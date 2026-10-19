"""Source adapter interface.

A *source adapter* wraps one external source (a search engine, an API, a portal) and
returns results in the common schema — or in the extended schema of its vertical.
Adapters are plain classes; the core never needs to know about a specific one.

To add a source, subclass :class:`SourceAdapter`, implement :meth:`search` and either
add it to a vertical's ``build_sources`` or expose a factory through the
``search_extract_api.sources`` entry point group (see ``docs/extending.md``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..core.cache import Cache
from ..core.http import HttpService
from ..schemas.common import BaseQuery, Result

if TYPE_CHECKING:
    from ..extraction.fetchers import FetchedPage
    from ..sessions.models import SessionHandle

SessionPolicy = Literal["none", "optional", "required"]


class PageFetcher:
    """Callable handed to adapters to render pages through the extraction pipeline
    (Crawl4AI / headless browser / light fetcher) with login and captcha detection."""

    async def __call__(
        self,
        url: str,
        *,
        session: SessionHandle | None = None,
        mode: str = "auto",
        lang: str | None = None,
    ) -> FetchedPage:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class SourceContext:
    settings: Settings
    http: HttpService
    cache: Cache
    fetch_page: PageFetcher | Any
    session: SessionHandle | None = None
    #: Free-form diagnostics an adapter wants to expose in ``sources[].details``.
    details: dict[str, Any] = field(default_factory=dict)


class SourceInfo(BaseModel):
    name: str
    title: str
    description: str = ""
    configured: bool
    fallback: bool = False
    default_enabled: bool = True
    session: SessionPolicy = "none"
    session_domain: str | None = None
    notes: list[str] = Field(default_factory=list)


class SourceAdapter(ABC):
    """Base class for every source."""

    #: Unique, stable identifier (used in ``sources=`` filters and ``providers``).
    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: Result model this adapter produces (must subclass :class:`Result`).
    result_model: ClassVar[type[Result]] = Result
    #: Whether the source needs an authenticated session.
    session_policy: ClassVar[SessionPolicy] = "none"
    #: Domain the session must belong to (e.g. ``linkedin.com``).
    session_domain: ClassVar[str | None] = None
    #: Commercial / backup provider, only used per the fallback policy.
    fallback: ClassVar[bool] = False
    #: Queried when the caller does not pick sources explicitly.
    default_enabled: ClassVar[bool] = True
    #: If True, a missing configuration (e.g. no API key) silently skips the source
    #: instead of reporting it as failed.
    optional_config: ClassVar[bool] = False
    #: Per-source timeout in seconds (None = ``SOURCE_TIMEOUT`` setting).
    timeout: ClassVar[float | None] = None

    def configured(self, settings: Settings) -> bool:
        """False if the source lacks required configuration (URL, API key...)."""
        return True

    def notes(self, settings: Settings) -> list[str]:
        return []

    @abstractmethod
    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        """Returns results in rank order. Raise :class:`ApiError` on failure."""

    def info(self, settings: Settings) -> SourceInfo:
        return SourceInfo(
            name=self.name,
            title=self.title or self.name,
            description=self.description,
            configured=self.configured(settings),
            fallback=self.fallback,
            default_enabled=self.default_enabled,
            session=self.session_policy,
            session_domain=self.session_domain,
            notes=self.notes(settings),
        )
