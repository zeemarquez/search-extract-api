"""A1 — RSS/Atom/JSON Feed reading and feed discovery."""

from __future__ import annotations

import httpx
import pytest

from search_extract_api.schemas.common import Result

pytestmark = pytest.mark.a1

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>Diario Ejemplo - Economía</title>
  <link>https://www.diarioejemplo.es/economia</link>
  <description>Últimas noticias de economía</description>
  <language>es-ES</language>
  <item>
    <title>El Gobierno aprueba el nuevo plan de vivienda</title>
    <link>https://www.diarioejemplo.es/economia/plan-vivienda?utm_source=rss&amp;utm_medium=feed</link>
    <description><![CDATA[<p>El Consejo de Ministros ha aprobado el <b>plan estatal</b> de vivienda.</p><img src="https://img.diarioejemplo.es/v.jpg">]]></description>
    <pubDate>Sat, 18 Oct 2026 09:30:00 +0200</pubDate>
    <dc:creator>Lucía Fernández</dc:creator>
    <category>Vivienda</category>
    <guid isPermaLink="false">de-123</guid>
  </item>
  <item>
    <title>El Euríbor sube por tercer mes consecutivo</title>
    <link>https://www.diarioejemplo.es/economia/euribor</link>
    <description>El indicador hipotecario vuelve a subir.</description>
    <pubDate>Fri, 17 Oct 2026 08:00:00 +0200</pubDate>
    <media:content url="https://img.diarioejemplo.es/euribor.jpg" medium="image"/>
  </item>
</channel>
</rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">
  <title>Example Blog</title>
  <link href="https://blog.example.com/"/>
  <updated>2026-10-18T12:00:00Z</updated>
  <id>urn:uuid:60a76c80</id>
  <entry>
    <title>Releasing version 2.0</title>
    <link href="https://blog.example.com/posts/v2"/>
    <id>urn:uuid:1225c695</id>
    <updated>2026-10-18T12:00:00Z</updated>
    <summary>We are happy to announce the release of version 2.0 with many improvements.</summary>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""

JSON_FEED = {
    "version": "https://jsonfeed.org/version/1.1",
    "title": "JSON Example",
    "home_page_url": "https://json.example.com/",
    "items": [
        {
            "id": "1",
            "url": "https://json.example.com/p/1",
            "title": "Hello JSON Feed",
            "content_text": "Body text",
            "date_published": "2026-10-10T10:00:00Z",
            "authors": [{"name": "Ann"}],
        }
    ],
}

HTML_WITH_FEEDS = """<html><head>
<title>Diario Ejemplo</title>
<link rel="alternate" type="application/rss+xml" title="Portada" href="/rss/portada.xml">
<link rel="alternate" type="application/atom+xml" title="Atom" href="https://www.diarioejemplo.es/atom.xml">
<link rel="alternate" type="application/json+oembed" href="/oembed?x=1">
</head><body><p>Hola</p></body></html>"""


async def test_read_rss(client, net) -> None:
    net.get("https://www.diarioejemplo.es/rss/economia.xml").mock(
        return_value=httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})
    )
    response = await client.get(
        "/feeds", params={"url": "https://www.diarioejemplo.es/rss/economia.xml"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feed"]["title"] == "Diario Ejemplo - Economía"
    assert body["feed"]["language"] == "es"
    assert body["feed"]["format"] == "rss20"
    assert body["count"] == 2
    first = Result.model_validate(body["results"][0])
    assert first.url == "https://www.diarioejemplo.es/economia/plan-vivienda"  # tracking removed
    assert first.snippet == "El Consejo de Ministros ha aprobado el plan estatal de vivienda."
    assert first.image == "https://img.diarioejemplo.es/v.jpg"
    assert first.language == "es"
    assert first.published_at.isoformat() == "2026-10-18T07:30:00+00:00"
    assert first.providers == ["feed:diarioejemplo.es"]
    assert first.extra["author"] == "Lucía Fernández"
    assert first.extra["tags"] == ["Vivienda"]
    second = Result.model_validate(body["results"][1])
    assert second.image == "https://img.diarioejemplo.es/euribor.jpg"


async def test_read_atom_and_json_feed(client, net) -> None:
    net.get("https://blog.example.com/atom.xml").mock(return_value=httpx.Response(200, text=ATOM))
    net.get("https://json.example.com/feed.json").mock(
        return_value=httpx.Response(200, json=JSON_FEED)
    )
    atom = (await client.get("/feeds", params={"url": "https://blog.example.com/atom.xml"})).json()
    assert atom["feed"]["format"] == "atom10"
    assert atom["results"][0]["url"] == "https://blog.example.com/posts/v2"
    assert atom["results"][0]["extra"]["author"] == "Jane Doe"
    assert atom["results"][0]["language"] == "en"
    jf = (await client.get("/feeds", params={"url": "https://json.example.com/feed.json"})).json()
    assert jf["feed"]["format"] == "jsonfeed"
    assert jf["results"][0]["title"] == "Hello JSON Feed"
    assert jf["results"][0]["extra"]["author"] == "Ann"


async def test_not_a_feed(client, net) -> None:
    net.get("https://example.com/page").mock(
        return_value=httpx.Response(200, text="<html><body>hi</body></html>")
    )
    response = await client.get("/feeds", params={"url": "https://example.com/page"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FEED_PARSE_ERROR"


async def test_feed_404(client, net) -> None:
    net.get("https://example.com/missing.xml").mock(return_value=httpx.Response(404))
    response = await client.get("/feeds", params={"url": "https://example.com/missing.xml"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FEED_NOT_FOUND"


async def test_batch_feeds_tolerate_failures(client, net) -> None:
    net.get("https://www.diarioejemplo.es/rss/economia.xml").mock(
        return_value=httpx.Response(200, text=RSS)
    )
    net.get("https://blog.example.com/atom.xml").mock(return_value=httpx.Response(200, text=ATOM))
    net.get("https://down.example.com/feed").mock(side_effect=httpx.ConnectTimeout("timeout"))
    response = await client.post(
        "/feeds",
        json={
            "urls": [
                "https://www.diarioejemplo.es/rss/economia.xml",
                "https://down.example.com/feed",
                "https://blog.example.com/atom.xml",
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["partial"] is True
    assert body["failed_sources"] == ["https://down.example.com/feed"]
    failed = next(s for s in body["sources"] if not s["ok"])
    assert failed["error"]["code"] == "FETCH_TIMEOUT"
    dates = [r["published_at"] for r in body["results"]]
    assert dates == sorted(dates, reverse=True)  # newest first


async def test_discover_from_link_tags(client, net) -> None:
    net.get("https://www.diarioejemplo.es/").mock(
        return_value=httpx.Response(200, text=HTML_WITH_FEEDS)
    )
    net.get("https://www.diarioejemplo.es/rss/portada.xml").mock(
        return_value=httpx.Response(200, text=RSS)
    )
    net.get("https://www.diarioejemplo.es/atom.xml").mock(return_value=httpx.Response(404))
    body = (
        await client.get("/feeds/discover", params={"url": "https://www.diarioejemplo.es/"})
    ).json()
    feeds = {f["url"]: f for f in body["feeds"]}
    assert set(feeds) == {
        "https://www.diarioejemplo.es/rss/portada.xml",
        "https://www.diarioejemplo.es/atom.xml",
    }
    portada = feeds["https://www.diarioejemplo.es/rss/portada.xml"]
    assert portada["verified"] is True
    assert portada["item_count"] == 2
    assert portada["found_by"] == "link"
    assert feeds["https://www.diarioejemplo.es/atom.xml"]["verified"] is False


async def test_discover_when_url_is_already_a_feed(client, net) -> None:
    net.get("https://blog.example.com/atom.xml").mock(
        return_value=httpx.Response(
            200, text=ATOM, headers={"content-type": "application/atom+xml"}
        )
    )
    body = (
        await client.get("/feeds/discover", params={"url": "https://blog.example.com/atom.xml"})
    ).json()
    assert body["feeds"][0]["found_by"] == "self"


async def test_discover_guesses_common_paths(client, net) -> None:
    net.get("https://plain.example.com/").mock(
        return_value=httpx.Response(200, text="<html><body>no feeds</body></html>")
    )
    net.get("https://plain.example.com/feed").mock(return_value=httpx.Response(200, text=RSS))
    net.route(host="plain.example.com").mock(return_value=httpx.Response(404))
    body = (
        await client.get("/feeds/discover", params={"url": "https://plain.example.com/"})
    ).json()
    assert [f["url"] for f in body["feeds"]] == ["https://plain.example.com/feed"]
    assert body["feeds"][0]["found_by"] == "guess"
