"""URL resolvers: turn redirector / aggregator links into the real document URL before
fetching (e.g. Google News article links -> publisher URL).

Resolvers are provided by verticals (``Vertical.url_resolvers()``), so the core stays
site-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..core.cache import Cache
    from ..core.http import HttpService


class UrlResolver(Protocol):
    name: str

    def matches(self, url: str) -> bool:
        """True if this resolver handles ``url``."""

    async def resolve(self, url: str, http: HttpService, cache: Cache) -> str | None:
        """Returns the resolved URL, or ``None`` if it cannot be resolved."""
