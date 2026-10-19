"""Contract of the common schema and the extended schemas across all verticals and
adapters (A1-A4), plus extensibility (new verticals/sources without core changes)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, ClassVar

import httpx
import pytest
import respx

from search_extract_api.adapters.base import SourceAdapter, SourceContext
from search_extract_api.config import Settings
from search_extract_api.core.cache import Cache, MemoryBackend
from search_extract_api.core.cookies import Cookie, StorageState
from search_extract_api.core.http import HttpService
from search_extract_api.core.urls import canonicalize_url, result_id
from search_extract_api.extraction.service import ExtractionService
from search_extract_api.schemas.common import BaseQuery, Result, SearchResponse
from search_extract_api.sessions.models import SessionHandle
from search_extract_api.verticals.base import Vertical, VerticalRegistry
from search_extract_api.verticals.housing.schemas import HousingQuery
from search_extract_api.verticals.jobs.schemas import JobsQuery
from search_extract_api.verticals.news.schemas import NewsQuery

from .conftest import SEARXNG, fixture_text, outgoing_client, searxng_item, searxng_payload
from .test_fallback import EXA_BODY, TAVILY_BODY
from .test_jobs import ARBEITNOW, REMOTIVE
from .test_news import GDELT_JSON, GNEWS_RSS

COMMON_FIELDS = {
    "id",
    "url",
    "title",
    "source",
    "published_at",
    "language",
    "snippet",
    "image",
    "providers",
}
EXTENDED_FIELDS = {
    "web": set(),
    "news": {"outlet", "author", "published_at"},
    "jobs": {"job_title", "company", "location", "work_mode", "published_at"},
    "housing": {"operation", "price", "area_m2", "rooms", "location"},
}
QUERIES: dict[str, BaseQuery] = {
    "web": BaseQuery(q="python"),
    "news": NewsQuery(q="vivienda"),
    "jobs": JobsQuery(q="python"),
    "housing": HousingQuery(location="madrid", latitude=40.4, longitude=-3.7),
}


def _html(name: str) -> httpx.Response:
    return httpx.Response(200, text=fixture_text(name), headers={"content-type": "text/html"})


def _searxng(net: respx.MockRouter) -> None:
    net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json=searxng_payload(
                [
                    searxng_item(
                        "https://example.com/a?utm_source=x",
                        "Piso en venta - 3 habitaciones, 90 m², 300.000 €",
                        "Snippet text",
                        publishedDate="2026-10-18T10:00:00",
                        author="Ann",
                    ),
                ]
            ),
        )
    )


# How to make each built-in source answer. A new built-in source must add an entry.
MOCKS: dict[str, Callable[[respx.MockRouter], None]] = {
    "searxng": _searxng,
    "searxng_news": _searxng,
    "web_jobs": _searxng,
    "web_housing": _searxng,
    "tavily": lambda net: net.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json=TAVILY_BODY)
    ),
    "tavily_news": lambda net: net.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json=TAVILY_BODY)
    ),
    "exa": lambda net: net.post("https://api.exa.ai/search").mock(
        return_value=httpx.Response(200, json=EXA_BODY)
    ),
    "exa_news": lambda net: net.post("https://api.exa.ai/search").mock(
        return_value=httpx.Response(200, json=EXA_BODY)
    ),
    "gdelt": lambda net: net.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
        return_value=httpx.Response(200, json=GDELT_JSON)
    ),
    "google_news": lambda net: net.get("https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, text=GNEWS_RSS)
    ),
    "linkedin": lambda net: (
        net.get("https://www.linkedin.com/feed/").mock(
            return_value=httpx.Response(
                200, text="<html><body>" + "<p>x</p>" * 300 + "</body></html>"
            )
        ),
        net.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search").mock(
            return_value=_html("linkedin_jobs.html")
        ),
    ),
    "remotive": lambda net: net.get("https://remotive.com/api/remote-jobs").mock(
        return_value=httpx.Response(200, json=REMOTIVE)
    ),
    "arbeitnow": lambda net: net.get("https://www.arbeitnow.com/api/job-board-api").mock(
        return_value=httpx.Response(200, json=ARBEITNOW)
    ),
    "idealista": lambda net: net.get(
        "https://www.idealista.com/venta-viviendas/madrid-madrid/"
    ).mock(return_value=_html("idealista_listing.html")),
    "idealista_api": lambda net: (
        net.post("https://api.idealista.com/oauth/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        ),
        net.post("https://api.idealista.com/3.5/es/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "elementList": [
                        {
                            "propertyCode": "1",
                            "price": 1000,
                            "size": 50,
                            "rooms": 1,
                            "url": "https://www.idealista.com/inmueble/1/",
                            "operation": "sale",
                        }
                    ]
                },
            )
        ),
    ),
}


def _registry(settings: Settings) -> VerticalRegistry:
    return VerticalRegistry.load(settings)


PHASE = {
    "web": pytest.mark.a1,
    "news": pytest.mark.a2,
    "jobs": pytest.mark.a4,
    "housing": pytest.mark.a4,
}
pytestmark = [pytest.mark.a1, pytest.mark.a2, pytest.mark.a3, pytest.mark.a4]


def _all_adapters(settings: Settings) -> list[Any]:
    registry = _registry(settings)
    return [
        pytest.param(
            name, adapter, id=f"{name}-{adapter.name}", marks=PHASE.get(name, pytest.mark.a1)
        )
        for name in registry.verticals
        for adapter in registry.sources_for(name)
    ]


def test_every_vertical_extends_the_common_schema(settings) -> None:
    registry = _registry(settings)
    assert set(registry.verticals) >= {"web", "news", "jobs", "housing"}
    for name, vertical in registry.verticals.items():
        assert issubclass(vertical.result_model, Result), name
        assert issubclass(vertical.query_model, BaseQuery), name
        fields = set(vertical.result_model.model_fields)
        assert COMMON_FIELDS <= fields, name
        assert EXTENDED_FIELDS.get(name, set()) <= fields, name
        schema = vertical.describe(registry.sources_for(name), settings)
        assert COMMON_FIELDS <= set(schema["result_schema"]["properties"]), name
        # The common filters are available in every vertical.
        assert {"lang", "region", "time_range", "since", "until"} <= set(
            vertical.query_model.model_fields
        )


def test_every_adapter_declares_a_compatible_schema(settings) -> None:
    registry = _registry(settings)
    for vertical_name, vertical in registry.verticals.items():
        names = [a.name for a in registry.sources_for(vertical_name)]
        assert len(names) == len(set(names)), f"duplicate source names in {vertical_name}"
        for adapter in registry.sources_for(vertical_name):
            assert issubclass(adapter.result_model, vertical.result_model), adapter.name
            info = adapter.info(settings)
            assert info.name == adapter.name and info.session in {"none", "optional", "required"}
            if adapter.session_policy != "none":
                assert adapter.session_domain, adapter.name


@pytest.mark.parametrize("vertical_name,adapter", _all_adapters(Settings(_env_file=None)))
async def test_adapter_output_honours_contract(
    vertical_name: str, adapter: SourceAdapter, settings
) -> None:
    assert adapter.name in MOCKS, f"add a contract mock for source '{adapter.name}'"
    settings = settings.model_copy(
        update={
            "tavily_api_key": "t",
            "exa_api_key": "e",
            "idealista_api_key": "k",
            "idealista_api_secret": "s",
        }
    )
    cache = Cache(MemoryBackend())
    async with outgoing_client() as client:
        http = HttpService(settings, cache, client=client)
        extraction = ExtractionService(settings=settings, http=http, cache=cache)
        session = None
        if adapter.session_policy == "required":
            session = SessionHandle(
                id="ses_test",
                domain=adapter.session_domain or "",
                owner_id="o",
                state=StorageState(
                    cookies=[
                        Cookie(
                            name="li_at",
                            value="v",
                            domain=f".{adapter.session_domain}",
                            expires=time.time() + 3600,
                        )
                    ]
                ),
            )
        ctx = SourceContext(
            settings=settings,
            http=http,
            cache=cache,
            fetch_page=extraction.fetch_page,
            session=session,
        )
        with respx.mock(assert_all_mocked=True) as net:
            MOCKS[adapter.name](net)
            results = await adapter.search(QUERIES[vertical_name], ctx)
    assert results, f"{adapter.name} returned nothing from its contract fixture"
    for result in results:
        assert isinstance(result, adapter.result_model)
        dumped = result.model_dump(mode="json")
        assert COMMON_FIELDS <= set(dumped)
        assert EXTENDED_FIELDS[vertical_name] <= set(dumped)
        assert type(result).model_validate(dumped) == result  # round trip
        assert result.url == canonicalize_url(result.url)
        assert result.id == result_id(result.url)
        assert result.providers[0] == adapter.name
    # And the merged response envelope validates against the vertical schema.
    envelope = SearchResponse[adapter.result_model](
        query={}, results=results, count=len(results), sources=[]
    )
    SearchResponse[adapter.result_model].model_validate(envelope.model_dump(mode="json"))


class EventResult(Result):
    venue: str | None = None


class EventQuery(BaseQuery):
    city: str | None = None


class EventsSource(SourceAdapter):
    name = "fake_events"
    result_model = EventResult

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        return [
            EventResult(
                url="https://events.example.com/1",
                title=f"Concert in {query.city}",
                venue="Arena",
                providers=[self.name],
            )
        ]


class EventsVertical(Vertical):
    name = "events"
    title = "Events"
    prefix = "/events"
    query_model = EventQuery
    result_model = EventResult
    sources: ClassVar[list[Any]] = []

    def build_sources(self, settings: Settings) -> list[SourceAdapter]:
        return [EventsSource()]


async def test_new_vertical_without_touching_the_core(settings) -> None:
    from search_extract_api.main import create_app

    app = create_app(settings, extra_verticals=[EventsVertical()])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            body = (await c.get("/events/search", params={"city": "Madrid"})).json()
            assert body["results"][0]["venue"] == "Arena"
            assert body["results"][0]["title"] == "Concert in Madrid"
            assert "/events/search" in (await c.get("/openapi.json")).json()["paths"]
            sources = (await c.get("/events/sources")).json()
            assert sources[0]["name"] == "fake_events"


def test_registry_rejects_incompatible_sources(settings) -> None:
    registry = _registry(settings)

    class WrongSource(EventsSource):
        name = "wrong"

    with pytest.raises(TypeError):
        registry.add_source("news", WrongSource())

    class ExtraNews(SourceAdapter):
        from search_extract_api.verticals.news.schemas import NewsResult as result_model

        name = "extra_news"

        async def search(self, query, ctx):  # pragma: no cover - not called
            return []

    registry.add_source("news", ExtraNews())
    assert "extra_news" in [a.name for a in registry.sources_for("news")]


def test_disabled_sources_and_verticals(settings) -> None:
    custom = settings.model_copy(update={"disabled_sources": "gdelt", "enabled_verticals": "news"})
    registry = VerticalRegistry.load(custom)
    assert set(registry.verticals) == {"web", "news"}
    assert "gdelt" not in [a.name for a in registry.sources_for("news")]
