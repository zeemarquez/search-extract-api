"""A2 — news vertical: SearXNG news + GDELT + Google News, merged across sources."""

from __future__ import annotations

import httpx
import pytest

from search_extract_api.verticals.news.schemas import NewsQuery, NewsResult
from search_extract_api.verticals.news.sources import GdeltAdapter, GoogleNewsAdapter

from .conftest import SEARXNG, searxng_item, searxng_payload

pytestmark = pytest.mark.a2

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS_SEARCH = "https://news.google.com/rss/search"

GNEWS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<title>"vivienda" - Google Noticias</title><link>https://news.google.com/</link>
<item>
  <title>El Gobierno aprueba el nuevo plan de vivienda - El País</title>
  <link>https://news.google.com/rss/articles/CBMiXEFVX3lxTE1hYmNkZWY?oc=5</link>
  <guid isPermaLink="false">CBMiXEFVX3lxTE1hYmNkZWY</guid>
  <pubDate>Sat, 18 Oct 2026 07:45:00 GMT</pubDate>
  <description>&lt;a href="https://news.google.com/rss/articles/CBMi"&gt;El Gobierno aprueba&lt;/a&gt;</description>
  <source url="https://elpais.com">El País</source>
</item>
<item>
  <title>La vivienda, principal preocupación de los jóvenes - El Mundo</title>
  <link>https://news.google.com/rss/articles/CBMiZZZZ?oc=5</link>
  <pubDate>Fri, 17 Oct 2026 10:00:00 GMT</pubDate>
  <source url="https://www.elmundo.es">El Mundo</source>
</item>
</channel></rss>"""

GDELT_JSON = {
    "articles": [
        {
            "url": "https://www.elpais.com/economia/2026-10-18/plan-vivienda.html?utm_source=gdelt",
            "url_mobile": "",
            "title": "El Gobierno aprueba el nuevo plan de vivienda",
            "seendate": "20261018T080000Z",
            "socialimage": "https://imagenes.elpais.com/plan.jpg",
            "domain": "elpais.com",
            "language": "Spanish",
            "sourcecountry": "Spain",
        },
        {
            "url": "https://www.lavanguardia.com/economia/vivienda-cataluna.html",
            "title": "Cataluña pide más fondos para vivienda",
            "seendate": "20261017T120000Z",
            "socialimage": "",
            "domain": "lavanguardia.com",
            "language": "Spanish",
            "sourcecountry": "Spain",
        },
    ]
}


def mock_searxng_news(net) -> None:
    net.get(f"{SEARXNG}/search", params={"categories": "news"}).mock(
        return_value=httpx.Response(
            200,
            json=searxng_payload(
                [
                    searxng_item(
                        "https://elpais.com/economia/2026-10-18/plan-vivienda.html",
                        "El Gobierno aprueba el nuevo plan de vivienda | El País",
                        "El Consejo de Ministros aprobó el plan...",
                        publishedDate="2026-10-18T07:40:00",
                        category="news",
                    ),
                ]
            ),
        )
    )


async def test_news_merges_sources(client, net) -> None:
    mock_searxng_news(net)
    gdelt = net.get(GDELT).mock(return_value=httpx.Response(200, json=GDELT_JSON))
    gnews = net.get(GNEWS_SEARCH).mock(return_value=httpx.Response(200, text=GNEWS_RSS))
    response = await client.get(
        "/news/search", params={"q": "vivienda", "lang": "es", "region": "ES", "time_range": "week"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["failed_sources"] == []
    results = [NewsResult.model_validate(r) for r in body["results"]]
    plan = [r for r in results if "plan de vivienda" in (r.title or "")]
    assert len(plan) == 1, "same story from three sources must be merged"
    story = plan[0]
    assert set(story.providers) == {"searxng_news", "gdelt", "google_news"}
    assert story.url == "https://elpais.com/economia/2026-10-18/plan-vivienda.html"
    assert story.outlet == "El País"
    assert story.image == "https://imagenes.elpais.com/plan.jpg"
    assert story.published_at is not None
    assert results[0].id == story.id  # found by most sources -> ranked first
    # Google News link that could not be matched keeps its outlet from <source>.
    mundo = next(r for r in results if r.outlet == "El Mundo")
    assert mundo.source == "elmundo.es"
    # Outlet defaults to the domain when no source provides a name.
    vanguardia = next(r for r in results if "lavanguardia" in r.url)
    assert vanguardia.outlet == "lavanguardia.com"
    # Filters are translated per source.
    gdelt_query = gdelt.calls.last.request.url.params["query"]
    assert "sourcelang:spanish" in gdelt_query and "sourcecountry:spain" in gdelt_query
    assert "startdatetime" in gdelt.calls.last.request.url.params
    gnews_params = gnews.calls.last.request.url.params
    assert (
        gnews_params["hl"] == "es"
        and gnews_params["gl"] == "ES"
        and gnews_params["ceid"] == "ES:es"
    )
    assert "when:" in gnews_params["q"]


async def test_news_tolerates_gdelt_text_errors(client, net) -> None:
    mock_searxng_news(net)
    net.get(GDELT).mock(
        return_value=httpx.Response(
            200,
            text="Your search contained a keyword that was too short.",
            headers={"content-type": "text/html"},
        )
    )
    net.get(GNEWS_SEARCH).mock(side_effect=httpx.ConnectError("down"))
    body = (await client.get("/news/search", params={"q": "vivienda"})).json()
    assert body["partial"] is True
    assert set(body["failed_sources"]) == {"gdelt", "google_news"}
    gdelt_status = next(s for s in body["sources"] if s["name"] == "gdelt")
    assert "too short" in gdelt_status["error"]["message"]
    assert body["count"] == 1  # SearXNG results still returned


async def test_news_domain_filters(client, net) -> None:
    mock_searxng_news(net)
    net.get(GDELT).mock(return_value=httpx.Response(200, json=GDELT_JSON))
    net.get(GNEWS_SEARCH).mock(return_value=httpx.Response(200, text=GNEWS_RSS))
    body = (
        await client.get("/news/search", params={"q": "vivienda", "exclude_domains": "elpais.com"})
    ).json()
    assert all("elpais" not in (r["source"] or "") for r in body["results"])
    body = (
        await client.get("/news/search", params={"q": "vivienda", "domains": "lavanguardia.com"})
    ).json()
    assert [r["source"] for r in body["results"]] == ["lavanguardia.com"]


async def test_news_top_headlines(client, net) -> None:
    route = net.get("https://news.google.com/rss/headlines/section/topic/TECHNOLOGY").mock(
        return_value=httpx.Response(200, text=GNEWS_RSS)
    )
    body = (
        await client.get("/news/top", params={"lang": "es", "region": "ES", "topic": "technology"})
    ).json()
    assert body["count"] == 2
    assert body["results"][0]["outlet"] == "El País"
    assert route.calls.last.request.url.params["ceid"] == "ES:es"


def test_gdelt_query_building() -> None:
    query = NewsQuery(
        q="huelga metro", lang="es", region="ES", domains=["elpais.com", "elmundo.es"]
    )
    built = GdeltAdapter.build_query(query)
    assert built == (
        "huelga metro sourcelang:spanish sourcecountry:spain "
        "(domainis:elpais.com OR domainis:elmundo.es)"
    )


def test_google_news_query_building() -> None:
    query = NewsQuery(q="huelga", time_range="day", exclude_domains=["example.com"])
    built = GoogleNewsAdapter.build_query(query)
    assert built.startswith("huelga when:")
    assert built.endswith("-site:example.com")
