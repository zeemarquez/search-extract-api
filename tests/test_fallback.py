"""A2 — commercial fallback providers (Tavily, Exa) and the fallback policy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from .conftest import SEARXNG, searxng_item, searxng_payload

pytestmark = pytest.mark.a2

TAVILY = "https://api.tavily.com/search"
EXA = "https://api.exa.ai/search"

RECENT = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

TAVILY_BODY = {
    "query": "q",
    "results": [
        {
            "title": "From Tavily",
            "url": "https://t.example.com/1",
            "content": "tavily snippet",
            "score": 0.9,
            "published_date": RECENT,
        },
    ],
}
EXA_BODY = {
    "results": [
        {
            "id": "x",
            "title": "From Exa",
            "url": "https://e.example.com/1",
            "publishedDate": f"{RECENT}T00:00:00.000Z",
            "author": "Ann",
            "highlights": ["exa highlight"],
            "image": "https://e.example.com/i.png",
        },
    ]
}


@pytest.fixture
async def keyed_client(make_client):
    async with make_client(tavily_api_key="tvly-test", exa_api_key="exa-test") as c:
        yield c


def searxng_with(net, count: int) -> None:
    net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json=searxng_payload(
                [searxng_item(f"https://s.example.com/{i}", f"S{i}") for i in range(count)]
            ),
        )
    )


async def test_auto_uses_fallback_when_primary_fails(keyed_client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(503))
    tavily = net.post(TAVILY).mock(return_value=httpx.Response(200, json=TAVILY_BODY))
    exa = net.post(EXA).mock(return_value=httpx.Response(200, json=EXA_BODY))
    body = (await keyed_client.get("/search", params={"q": "x", "time_range": "week"})).json()
    assert tavily.called and exa.called
    urls = {r["url"] for r in body["results"]}
    assert urls == {"https://t.example.com/1", "https://e.example.com/1"}
    assert body["failed_sources"] == ["searxng"]
    statuses = {s["name"]: s for s in body["sources"]}
    assert statuses["tavily"]["ok"] and statuses["tavily"]["fallback"] is True
    # Provider-specific request formats.
    tavily_request = tavily.calls.last.request
    assert tavily_request.headers["authorization"] == "Bearer tvly-test"
    assert json.loads(tavily_request.content)["time_range"] == "week"
    exa_request = exa.calls.last.request
    assert exa_request.headers["x-api-key"] == "exa-test"
    assert "startPublishedDate" in json.loads(exa_request.content)
    exa_result = next(r for r in body["results"] if r["url"].startswith("https://e."))
    assert exa_result["snippet"] == "exa highlight"


async def test_auto_skips_fallback_when_primary_is_enough(keyed_client, net) -> None:
    searxng_with(net, 5)
    tavily = net.post(TAVILY).mock(return_value=httpx.Response(200, json=TAVILY_BODY))
    body = (await keyed_client.get("/search", params={"q": "x"})).json()
    assert not tavily.called
    assert {s["name"] for s in body["sources"]} == {"searxng"}


async def test_auto_uses_fallback_when_too_few_results(keyed_client, net) -> None:
    searxng_with(net, 1)
    tavily = net.post(TAVILY).mock(return_value=httpx.Response(200, json=TAVILY_BODY))
    net.post(EXA).mock(return_value=httpx.Response(200, json=EXA_BODY))
    body = (await keyed_client.get("/search", params={"q": "x", "limit": 10})).json()
    assert tavily.called
    assert body["count"] == 3
    assert body["partial"] is False


async def test_never_and_always_modes(keyed_client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(503))
    tavily = net.post(TAVILY).mock(return_value=httpx.Response(200, json=TAVILY_BODY))
    net.post(EXA).mock(return_value=httpx.Response(200, json=EXA_BODY))
    body = (await keyed_client.get("/search", params={"q": "x", "fallback": "never"})).json()
    assert not tavily.called
    assert body["count"] == 0 and body["failed_sources"] == ["searxng"]

    net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json=searxng_payload(
                [searxng_item(f"https://s.example.com/{i}", f"S{i}") for i in range(5)]
            ),
        )
    )
    body = (await keyed_client.get("/search", params={"q": "y", "fallback": "always"})).json()
    assert tavily.called
    assert {s["name"] for s in body["sources"]} == {"searxng", "tavily", "exa"}


async def test_explicit_sources(keyed_client, net) -> None:
    searx = net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(200, json=searxng_payload([]))
    )
    exa = net.post(EXA).mock(return_value=httpx.Response(200, json=EXA_BODY))
    body = (await keyed_client.get("/search", params={"q": "x", "sources": "exa"})).json()
    assert exa.called and not searx.called
    assert [s["name"] for s in body["sources"]] == ["exa"]


async def test_explicit_unconfigured_source_fails(client, net) -> None:
    body = (await client.get("/search", params={"q": "x", "sources": "tavily"})).json()
    assert body["failed_sources"] == ["tavily"]
    assert body["sources"][0]["error"]["code"] == "SOURCE_NOT_CONFIGURED"


async def test_news_fallbacks_use_news_mode(make_client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(return_value=httpx.Response(503))
    net.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(return_value=httpx.Response(503))
    net.get("https://news.google.com/rss/search").mock(return_value=httpx.Response(503))
    tavily = net.post(TAVILY).mock(return_value=httpx.Response(200, json=TAVILY_BODY))
    exa = net.post(EXA).mock(return_value=httpx.Response(200, json=EXA_BODY))
    async with make_client(tavily_api_key="tvly-test", exa_api_key="exa-test") as c:
        body = (await c.get("/news/search", params={"q": "x"})).json()
    assert json.loads(tavily.calls.last.request.content)["topic"] == "news"
    assert json.loads(exa.calls.last.request.content)["category"] == "news"
    exa_result = next(r for r in body["results"] if r["url"].startswith("https://e."))
    assert exa_result["author"] == "Ann"
    assert exa_result["outlet"] == "e.example.com"
    assert set(body["failed_sources"]) == {"searxng_news", "gdelt", "google_news"}
