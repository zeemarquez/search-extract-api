# Extending: new sources and verticals

The core never needs to change to support a new site or domain. There are two extension
points, both plain Python classes that can live in **another package** and be discovered
through entry points.

* A **source adapter** wraps one external source and returns results in the schema of its
  vertical.
* A **vertical** is a module with its own endpoint prefix, query filters, extended result
  schema and list of sources.

## Adding a source to an existing vertical

```python
# my_package/sources.py
from search_extract_api.adapters.base import SourceAdapter, SourceContext
from search_extract_api.errors import ApiError, ErrorCode
from search_extract_api.verticals.news.schemas import NewsQuery, NewsResult


class HackerNewsSource(SourceAdapter):
    name = "hackernews"                 # unique, used in ?sources= and in `providers`
    title = "Hacker News (Algolia)"
    result_model = NewsResult           # must be the vertical's result model (or a subclass)
    vertical = "news"                   # which vertical to join (used by the entry point)

    async def search(self, query: NewsQuery, ctx: SourceContext) -> list[NewsResult]:
        data = await ctx.http.get_json(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"query": query.q, "tags": "story", "hitsPerPage": query.limit},
            source="Hacker News",
        )
        return [
            NewsResult(
                url=hit["url"],
                title=hit["title"],
                published_at=hit["created_at"],
                author=hit.get("author"),
                outlet="Hacker News",
                providers=[self.name],
            )
            for hit in data.get("hits", [])
            if hit.get("url")
        ]


def build(settings):
    return HackerNewsSource()
```

Register it in your package's `pyproject.toml`:

```toml
[project.entry-points."search_extract_api.sources"]
hackernews = "my_package.sources:build"
```

`pip install` your package next to `search-extract-api` and restart: `/news/search` now also
queries Hacker News, `/news/sources` lists it, and failures are isolated like any other
source.

What an adapter gets in `ctx`:

| Attribute | Use |
| --- | --- |
| `ctx.http.get_json(...)` | Call JSON APIs; HTTP errors become `SOURCE_*` errors |
| `ctx.http.fetch(...)` | Raw HTTP with size limits, SSRF guard and cookies |
| `ctx.fetch_page(url, session=..., mode=...)` | Render a page through Crawl4AI / headless browser / light fetcher **with login and captcha detection** |
| `ctx.session` | The caller's session, only when its domain matches `session_domain` |
| `ctx.http.politeness.wait_key(key, seconds)` | Respect an API's rate limit |
| `ctx.cache` | Temporary cache (TTL) |
| `ctx.details` | Diagnostics shown in `sources[].details` |

Adapter class attributes:

| Attribute | Meaning |
| --- | --- |
| `session_policy` | `"none"`, `"optional"` or `"required"` (+ `session_domain`) |
| `fallback` | Commercial/backup source used according to the fallback policy |
| `default_enabled` | Queried when the caller does not pass `sources` |
| `optional_config` + `configured()` | Skip silently when e.g. an API key is missing |
| `timeout` | Per-source timeout |

Rules of thumb: raise `ApiError` with a meaningful code instead of returning `[]` when the
page is not what you expected (`PARSE_ERROR` when the layout changed, `SESSION_EXPIRED`
when logged out); never log cookies or tokens; prefer official APIs and feeds over scraping.

## Adding a vertical

```python
# my_package/events.py
from pydantic import Field

from search_extract_api.adapters.base import SourceAdapter
from search_extract_api.schemas.common import BaseQuery, Result
from search_extract_api.verticals.base import Vertical


class EventResult(Result):
    venue: str | None = None
    starts_at: str | None = None


class EventsQuery(BaseQuery):
    city: str | None = Field(default=None, description="City name")


class EventsVertical(Vertical):
    name = "events"
    title = "Events"
    description = "Concerts and events."
    prefix = "/events"
    query_model = EventsQuery
    result_model = EventResult

    def build_sources(self, settings) -> list[SourceAdapter]:
        return [MyTicketingSource(), ...]

    # Optional hooks:
    # def postprocess(self, results, query): ...      # filters after merging
    # def session_profiles(self): ...                 # login/captcha detection per site
    # def url_resolvers(self): ...                    # turn redirector links into real URLs
    # def extra_routes(self, router): ...             # endpoints beyond /search and /sources
```

```toml
[project.entry-points."search_extract_api.verticals"]
events = "my_package.events:EventsVertical"
```

The vertical gets `GET/POST /events/search` (query parameters generated from
`EventsQuery`), `GET /events/sources`, OpenAPI documentation and the common machinery:
concurrent sources, merging, deduplication, caching, fault tolerance and sessions.

## Session profiles for new sites

If a source logs into a site, describe how the site behaves so expired sessions and
captchas are detected reliably:

```python
from search_extract_api.sessions.profiles import DomainProfile

PROFILE = DomainProfile(
    domain="example.com",
    check_url="https://example.com/account",          # only renders when logged in
    login_url="https://example.com/login",            # default page for interactive login
    auth_cookies=("session_id",),                     # must exist for a valid session
    login_path_patterns=(r"^/login", r"^/sso/"),      # redirects here mean "logged out"
    logged_out_markers=(r"Please sign in to continue",),
    captcha_markers=(r"captcha\.example\.com",),
)
```

Return it from your vertical's `session_profiles()`.

## URL resolvers

Some sources return redirector links instead of the document URL (Google News does). A
vertical can provide resolvers that `/extract` (and anything built on it) applies before
fetching:

```python
class MyResolver:
    name = "my_redirector"

    def matches(self, url: str) -> bool:
        return url.startswith("https://go.example.com/")

    async def resolve(self, url, http, cache) -> str | None:
        ...  # return the real URL (cache it), or None if it cannot be resolved
```

Return instances from `url_resolvers()`; see `verticals/news/google_news.py` for a full
example.

## Contract tests

`tests/test_contracts.py` runs every registered adapter against a canned response and checks
the common and extended schemas. When you add a built-in source, add its mock to `MOCKS`.
