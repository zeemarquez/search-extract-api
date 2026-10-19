"""A1 — general web search: normalization, fault tolerance and caching."""

from __future__ import annotations

import httpx
import pytest

from search_extract_api.schemas.common import Result

from .conftest import SEARXNG, searxng_item, searxng_payload

pytestmark = pytest.mark.a1


async def test_search_returns_common_schema(client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json=searxng_payload(
                [
                    searxng_item(
                        "https://www.example.com/a?utm_source=x",
                        "<b>Example</b> A",
                        "First result snippet that is long enough to detect the language.",
                        publishedDate="2026-10-18T10:00:00",
                        img_src="https://img.example.com/a.jpg",
                        engines=["google", "bing"],
                    ),
                    searxng_item("https://other.org/b", "Other B", "Second"),
                ]
            ),
        )
    )
    response = await client.get("/search", params={"q": "example", "lang": "en"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["partial"] is False
    assert body["failed_sources"] == []
    first = Result.model_validate(body["results"][0])
    assert first.url == "https://www.example.com/a"
    assert first.title == "Example A"
    assert first.source == "example.com"
    assert first.providers == ["searxng"]
    assert first.image == "https://img.example.com/a.jpg"
    assert first.published_at is not None
    assert first.extra["engines"] == ["google", "bing"]
    assert {
        "id",
        "url",
        "title",
        "source",
        "published_at",
        "language",
        "snippet",
        "image",
        "providers",
    } <= set(body["results"][0])
    # Language and time range are forwarded to SearXNG.
    sent = net.calls.last.request.url.params
    assert sent["format"] == "json"
    assert sent["language"] == "en"


async def test_failing_source_never_fails_the_request(client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(500, text="boom"))
    response = await client.get("/search", params={"q": "anything"})
    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert body["partial"] is True
    assert body["failed_sources"] == ["searxng"]
    status = body["sources"][0]
    assert status["ok"] is False
    assert status["error"]["code"] == "SOURCE_ERROR"


async def test_unreachable_source_is_reported(client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(side_effect=httpx.ConnectError("refused"))
    body = (await client.get("/search", params={"q": "anything"})).json()
    assert body["failed_sources"] == ["searxng"]
    assert body["sources"][0]["error"]["code"] == "SOURCE_ERROR"


async def test_all_engines_unresponsive_is_a_failure(client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200, json=searxng_payload([], unresponsive=[["google", "CAPTCHA"], ["bing", "timeout"]])
        )
    )
    body = (await client.get("/search", params={"q": "x"})).json()
    assert body["failed_sources"] == ["searxng"]
    assert "google" in body["sources"][0]["error"]["message"]
    assert body["sources"][0]["details"]["unresponsive_engines"]["google"] == "CAPTCHA"


async def test_search_results_are_cached(client, net) -> None:
    route = net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200, json=searxng_payload([searxng_item("https://a.com/", "A")])
        )
    )
    first = (await client.get("/search", params={"q": "cache me", "limit": 10})).json()
    calls = route.call_count
    second = (await client.get("/search", params={"q": "cache me", "limit": 10})).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert route.call_count == calls == 1
    await client.get("/search", params={"q": "cache me", "limit": 10, "no_cache": "true"})
    assert route.call_count == 2


async def test_failed_searches_are_not_cached(client, net) -> None:
    route = net.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(502))
    await client.get("/search", params={"q": "fail", "limit": 10})
    await client.get("/search", params={"q": "fail", "limit": 10})
    assert route.call_count == 2


async def test_post_search_and_date_filter(client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json=searxng_payload(
                [
                    searxng_item("https://a.com/old", "Old", publishedDate="2020-01-01T00:00:00"),
                    searxng_item("https://a.com/new", "New", publishedDate="2026-10-18T00:00:00"),
                    searxng_item("https://a.com/undated", "Undated"),
                ]
            ),
        )
    )
    response = await client.post(
        "/search", json={"q": "dated", "since": "2026-01-01T00:00:00Z", "sort": "date"}
    )
    assert response.status_code == 200
    urls = [r["url"] for r in response.json()["results"]]
    assert urls == ["https://a.com/new", "https://a.com/undated"]


async def test_unknown_source_is_a_client_error(client, net) -> None:
    response = await client.get("/search", params={"q": "x", "sources": "nope"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SOURCE_UNKNOWN"


async def test_validation_errors_use_common_envelope(client) -> None:
    response = await client.get("/search", params={"q": "x", "region": "Spain"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    response = await client.get("/search", params={"q": "x", "unknown_param": "1"})
    assert response.status_code == 422


async def test_searxng_not_configured_is_reported(make_client, net) -> None:
    async with make_client(searxng_url=None) as c:
        body = (await c.get("/search", params={"q": "x"})).json()
    assert body["failed_sources"] == ["searxng"]
    assert body["sources"][0]["error"]["code"] == "SOURCE_NOT_CONFIGURED"
    # Fallback providers without keys are skipped, not failed.
    assert all(s["skipped"] for s in body["sources"] if s["name"] in {"tavily", "exa"})


async def test_sources_listing(client) -> None:
    sources = (await client.get("/search/sources")).json()
    names = {s["name"]: s for s in sources}
    assert names["searxng"]["configured"] is True
    assert names["tavily"]["fallback"] is True
    assert names["tavily"]["configured"] is False


async def test_health_and_root(client) -> None:
    health = (await client.get("/health")).json()
    assert health["status"] in {"ok", "degraded"}
    assert health["components"]["cache"]["backend"] == "memory"
    root = (await client.get("/")).json()
    assert set(root["verticals"]) >= {
        "web",
        "news",
        "linkedin",
        "idealista",
        "remotive",
        "arbeitnow",
    }
    verticals = (await client.get("/verticals")).json()
    assert {v["name"] for v in verticals} >= {
        "web",
        "news",
        "linkedin",
        "idealista",
        "remotive",
        "arbeitnow",
    }


async def test_safesearch_from_query_string(client, net) -> None:
    route = net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200, json=searxng_payload([searxng_item("https://a.com/", "A")])
        )
    )
    response = await client.get(
        "/search",
        params={
            "q": "euribor",
            "limit": 20,
            "page": 1,
            "sort": "relevance",
            "no_cache": "false",
            "safesearch": "1",
        },
    )
    assert response.status_code == 200
    assert route.calls.last.request.url.params["safesearch"] == "1"
    bad = await client.get("/search", params={"q": "x", "safesearch": "3"})
    assert bad.status_code == 422
