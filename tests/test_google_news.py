"""A2 — resolving Google News redirect links to publisher URLs."""

from __future__ import annotations

import base64

import httpx
import pytest

from search_extract_api.verticals.news.google_news import (
    article_id,
    decode_offline,
    parse_batchexecute,
)

from .conftest import fixture_text
from .test_news import GNEWS_RSS

pytestmark = pytest.mark.a2

NEW_ID = "CBMi5gFBVV95cUxQNFVCd1JkX1B2OG1fbm1pdldleDYxMU05YVNnQWRDdGt3OGNzTXhsVGh0T2d5d1QzQllsRk0w"
GN_URL = f"https://news.google.com/rss/articles/{NEW_ID}?oc=5"
PUBLISHER = "https://www.diarioejemplo.es/economia/plan-vivienda"
ARTICLE_PAGE = (
    "<html><body><c-wiz><div jscontroller='aLI87' "
    f'data-n-a-id="{NEW_ID}" data-n-a-ts="1792413223" data-n-a-sg="AbIaSL_z4acDOx">'
    "</div></c-wiz></body></html>"
)
BATCH_RESPONSE = (
    ")]}'\n\n"
    '[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"' + PUBLISHER + '\\",1]",null,null,null,"generic"],'
    '["di",18],["af.httprm",17,"5076506837454136550",28]]'
)


def mock_google(net) -> tuple:
    page = net.get(f"https://news.google.com/rss/articles/{NEW_ID}").mock(
        return_value=httpx.Response(200, text=ARTICLE_PAGE)
    )
    batch = net.post("https://news.google.com/_/DotsSplashUi/data/batchexecute").mock(
        return_value=httpx.Response(200, text=BATCH_RESPONSE)
    )
    return page, batch


def test_article_id_and_offline_decoding() -> None:
    assert article_id(GN_URL) == NEW_ID
    assert article_id("https://news.google.com/read/CBMiABCDEFGHIJKL?hl=es") == "CBMiABCDEFGHIJKL"
    assert article_id("https://example.com/rss/articles/CBMiABCDEFGHIJKL") is None
    old = base64.urlsafe_b64encode(
        b"\x08\x13\x22\x2bhttps://www.example.com/old-style-article\xd2\x01\x00"
    ).decode()
    assert decode_offline(old.rstrip("=")) == "https://www.example.com/old-style-article"
    assert decode_offline(NEW_ID) is None  # new format needs the online decoder


def test_parse_batchexecute() -> None:
    assert parse_batchexecute(BATCH_RESPONSE) == PUBLISHER
    assert parse_batchexecute(')]}\'\n\n[["wrb.fr","Fbv4je",null]]') is None


async def test_extract_resolves_google_news_links(client, net) -> None:
    page, batch = mock_google(net)
    publisher = net.get(PUBLISHER).mock(
        return_value=httpx.Response(
            200, text=fixture_text("article_v1.html"), headers={"content-type": "text/html"}
        )
    )
    body = (await client.post("/extract", json={"url": GN_URL})).json()
    assert body["url"] == PUBLISHER
    assert body["requested_url"] == GN_URL
    assert body["title"] == "El Gobierno aprueba el nuevo plan de vivienda"
    assert page.calls.last.request.url.params["ucbcb"] == "1"  # skips the consent wall
    assert "f.req=" in batch.calls.last.request.content.decode()
    # The mapping is cached: a second extraction does not call Google again.
    await client.post("/extract", json={"url": GN_URL, "no_cache": True})
    assert page.call_count == 1 and batch.call_count == 1 and publisher.call_count == 2


async def test_unresolvable_link_is_an_error(client, net) -> None:
    net.get(f"https://news.google.com/rss/articles/{NEW_ID}").mock(
        return_value=httpx.Response(200, text="<html><body>nothing</body></html>")
    )
    response = await client.post("/extract", json={"url": GN_URL})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "FETCH_FAILED"


async def test_news_search_can_resolve_links(client, net) -> None:
    net.get("https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, text=GNEWS_RSS.replace("CBMiXEFVX3lxTE1hYmNkZWY", NEW_ID))
    )
    mock_google(net)
    net.post("https://news.google.com/_/DotsSplashUi/data/batchexecute").mock(
        side_effect=[httpx.Response(200, text=BATCH_RESPONSE), httpx.Response(500)]
    )
    net.get("https://news.google.com/rss/articles/CBMiZZZZ").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )
    body = (
        await client.get(
            "/news/search",
            params={"q": "vivienda", "sources": "google_news", "resolve_links": "true"},
        )
    ).json()
    urls = [r["url"] for r in body["results"]]
    assert PUBLISHER in urls
    resolved = next(r for r in body["results"] if r["url"] == PUBLISHER)
    assert resolved["extra"]["google_news_url"].startswith("https://news.google.com/rss/articles/")
    assert resolved["outlet"] == "El País"
    # A link that could not be resolved keeps its Google News URL instead of failing.
    assert any(u.startswith("https://news.google.com/") for u in urls)


async def test_consent_wall_is_not_content(client, net) -> None:
    net.get("https://www.example-news.com/a").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://consent.google.com/m?continue=x"}
        )
    )
    net.get("https://consent.google.com/m?continue=x").mock(
        return_value=httpx.Response(
            200,
            text="<html><head><title>Before you continue</title></head><body>Accept all</body></html>",
        )
    )
    response = await client.post("/extract", json={"url": "https://www.example-news.com/a"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ACCESS_BLOCKED"
