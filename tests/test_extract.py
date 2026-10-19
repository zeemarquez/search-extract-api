"""A1 — extraction: clean Markdown, metadata, fingerprint, fallbacks and block detection."""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from .conftest import fixture_text

pytestmark = pytest.mark.a1

ARTICLE_URL = "https://www.diarioejemplo.es/economia/plan-vivienda"
CRAWL4AI = "http://crawl4ai.test"


def html_response(name: str = "article_v1.html", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, text=fixture_text(name), headers={"content-type": "text/html; charset=utf-8"}
    )


async def test_extract_light(client, net) -> None:
    net.get(ARTICLE_URL).mock(return_value=html_response())
    response = await client.post("/extract", json={"url": ARTICLE_URL})
    assert response.status_code == 200
    body = response.json()
    assert body["fetcher"] == "light"
    assert body["extractor"] == "trafilatura"
    assert body["title"] == "El Gobierno aprueba el nuevo plan de vivienda"
    assert body["author"] == "Lucía Fernández"
    assert body["published_at"] == "2026-10-18T07:30:00Z"
    assert body["language"] == "es"
    assert body["site_name"] == "Diario Ejemplo"
    assert body["image"] == "https://www.diarioejemplo.es/img/vivienda.jpg"
    assert body["canonical_url"] == ARTICLE_URL
    md = body["markdown"]
    assert md.startswith("# El Gobierno aprueba")
    assert "3.500 millones" in md
    for noise in ("zapatillas", "Seguro de hogar", "Euríbor", "cookies", "Compartir", "Portada"):
        assert noise not in md
    # Tracking parameters are stripped from links inside the Markdown.
    assert "utm_" not in md and "fbclid" not in md
    assert body["fingerprint"]["hash"].startswith("sha256:")
    assert len(body["fingerprint"]["simhash"]) == 16
    assert body["word_count"] > 50


async def test_get_variant_and_cache(client, net) -> None:
    route = net.get(ARTICLE_URL).mock(return_value=html_response())
    first = (await client.get("/extract", params={"url": ARTICLE_URL})).json()
    second = (await client.get("/extract", params={"url": ARTICLE_URL})).json()
    assert route.call_count == 1
    assert first["cached"] is False and second["cached"] is True
    assert first["fingerprint"] == second["fingerprint"]


async def test_change_detection_against_previous_fingerprint(client, net) -> None:
    net.get(ARTICLE_URL).mock(
        side_effect=[
            html_response("article_v1.html"),
            html_response("article_v2_ads_changed.html"),
            html_response("article_v3_content_changed.html"),
        ]
    )
    v1 = (await client.post("/extract", json={"url": ARTICLE_URL, "no_cache": True})).json()
    previous = {
        "previous_fingerprint": v1["fingerprint"]["hash"],
        "previous_simhash": v1["fingerprint"]["simhash"],
    }
    v2 = (
        await client.post("/extract", json={"url": ARTICLE_URL, "no_cache": True, **previous})
    ).json()
    assert v2["changed"] is False
    assert v2["similarity"] == 1.0
    v3 = (
        await client.post("/extract", json={"url": ARTICLE_URL, "no_cache": True, **previous})
    ).json()
    assert v3["changed"] is True
    assert 0.7 < v3["similarity"] < 1.0


async def test_crawl4ai_primary_and_fingerprint_independent_of_fetcher(make_client, net) -> None:
    html = fixture_text("article_v1.html")
    crawl = net.post(f"{CRAWL4AI}/crawl").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "results": [
                    {
                        "url": ARTICLE_URL,
                        "success": True,
                        "status_code": 200,
                        "html": html,
                        "markdown": {"raw_markdown": "# raw", "fit_markdown": "# fit"},
                        "metadata": {"title": "El Gobierno aprueba el nuevo plan de vivienda"},
                        "redirected_url": ARTICLE_URL,
                    }
                ],
            },
        )
    )
    net.get(ARTICLE_URL).mock(return_value=html_response())
    async with make_client(crawl4ai_url=CRAWL4AI, crawl4ai_token="secret-token") as c:
        via_crawl4ai = (await c.post("/extract", json={"url": ARTICLE_URL})).json()
        via_light = (await c.post("/extract", json={"url": ARTICLE_URL, "mode": "light"})).json()
    assert via_crawl4ai["fetcher"] == "crawl4ai"
    assert via_light["fetcher"] == "light"
    assert via_crawl4ai["fingerprint"]["hash"] == via_light["fingerprint"]["hash"]
    sent = crawl.calls.last.request
    assert sent.headers["authorization"] == "Bearer secret-token"
    payload = json.loads(sent.content)
    assert payload["urls"] == [ARTICLE_URL]
    assert payload["crawler_config"]["params"]["cache_mode"] == "bypass"
    assert "cookies" not in json.dumps(payload["browser_config"])


async def test_crawl4ai_down_falls_back_to_light(make_client, net) -> None:
    net.post(f"{CRAWL4AI}/crawl").mock(side_effect=httpx.ConnectError("down"))
    net.get(ARTICLE_URL).mock(return_value=html_response())
    async with make_client(crawl4ai_url=CRAWL4AI) as c:
        body = (await c.post("/extract", json={"url": ARTICLE_URL})).json()
    assert body["fetcher"] == "light"
    assert "3.500 millones" in body["markdown"]


async def test_crawl4ai_failed_result_falls_back(make_client, net) -> None:
    net.post(f"{CRAWL4AI}/crawl").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "results": [
                    {
                        "url": ARTICLE_URL,
                        "success": False,
                        "error_message": "net::ERR_ABORTED",
                        "html": "",
                    }
                ],
            },
        )
    )
    net.get(ARTICLE_URL).mock(return_value=html_response())
    async with make_client(crawl4ai_url=CRAWL4AI) as c:
        body = (await c.post("/extract", json={"url": ARTICLE_URL})).json()
    assert body["fetcher"] == "light"


async def test_full_content_mode(client, net) -> None:
    net.get(ARTICLE_URL).mock(return_value=html_response())
    body = (
        await client.post(
            "/extract", json={"url": ARTICLE_URL, "content": "full", "include_links": True}
        )
    ).json()
    assert body["content"] == "full"
    assert body["extractor"] == "markdownify"
    assert "3.500 millones" in body["markdown"]
    assert any(
        link["url"] == "https://www.diarioejemplo.es/economia/inquilinos" for link in body["links"]
    )


async def test_captcha_page_is_an_error_not_content(client, net) -> None:
    challenge = (
        "<html><head><title>Just a moment...</title></head><body>"
        "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script>"
        "Checking your browser</body></html>"
    )
    net.get("https://protected.example.com/a").mock(
        return_value=httpx.Response(403, text=challenge)
    )
    response = await client.post("/extract", json={"url": "https://protected.example.com/a"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CAPTCHA_REQUIRED"


async def test_login_wall_is_an_error(client, net) -> None:
    net.get("https://members.example.com/report").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://members.example.com/login?next=/report"}
        )
    )
    net.get("https://members.example.com/login?next=/report").mock(
        return_value=httpx.Response(
            200, text="<html><body><form><input type='password'></form></body></html>"
        )
    )
    response = await client.post("/extract", json={"url": "https://members.example.com/report"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "LOGIN_REQUIRED"
    assert "login" in error["details"]["reason"]


async def test_upstream_http_error(client, net) -> None:
    net.get("https://example.com/gone").mock(
        return_value=httpx.Response(404, text="<html><body>Not found</body></html>")
    )
    response = await client.post("/extract", json={"url": "https://example.com/gone"})
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_HTTP_ERROR"
    assert error["details"]["status_code"] == 404


async def test_unsupported_content(client, net) -> None:
    net.get("https://example.com/file.pdf").mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}
        )
    )
    response = await client.post("/extract", json={"url": "https://example.com/file.pdf"})
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_CONTENT"


async def test_invalid_url(client) -> None:
    response = await client.post("/extract", json={"url": "ftp://example.com/x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_URL"


async def test_ssrf_guard_blocks_private_addresses(make_client, net, monkeypatch) -> None:
    async with make_client(allow_private_urls=False) as c:
        for url in (
            "http://127.0.0.1:6379/",
            "http://localhost/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://service.internal/",
        ):
            response = await c.post("/extract", json={"url": url})
            assert response.status_code == 400, url
            assert response.json()["error"]["code"] == "BLOCKED_URL", url

        async def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

        import asyncio

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        response = await c.post("/extract", json={"url": "https://looks-public.example.com/"})
        assert response.json()["error"]["code"] == "BLOCKED_URL"


async def test_redirect_to_private_address_is_blocked(make_client, net, monkeypatch) -> None:
    import asyncio

    async with make_client(allow_private_urls=False) as c:

        async def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
        net.get("https://public.example.com/r").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        )
        response = await c.post("/extract", json={"url": "https://public.example.com/r"})
    assert response.json()["error"]["code"] == "BLOCKED_URL"


async def test_batch_extraction(client, net) -> None:
    net.get(ARTICLE_URL).mock(return_value=html_response())
    net.get("https://example.com/missing").mock(return_value=httpx.Response(404))
    body = (
        await client.post(
            "/extract/batch", json={"urls": [ARTICLE_URL, "https://example.com/missing"]}
        )
    ).json()
    assert body["succeeded"] == 1 and body["failed"] == 1
    ok, failed = body["items"]
    assert ok["ok"] is True and ok["result"]["title"]
    assert failed["ok"] is False and failed["error"]["code"] == "UPSTREAM_HTTP_ERROR"


async def test_empty_page_is_extraction_failed(client, net) -> None:
    net.get("https://example.com/empty").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )
    response = await client.post("/extract", json={"url": "https://example.com/empty"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EXTRACTION_FAILED"


ROBOT_403 = "<html><body>Please respect our robot policy when crawling us.</body></html>"


async def test_bot_blocked_browser_ua_retries_with_honest_crawler_ua(client, net) -> None:
    article = fixture_text("article_v1.html")

    def by_user_agent(request: httpx.Request) -> httpx.Response:
        if request.headers["user-agent"].startswith("Mozilla/"):
            return httpx.Response(403, text=ROBOT_403, headers={"content-type": "text/html"})
        return httpx.Response(200, text=article, headers={"content-type": "text/html"})

    route = net.get("https://wiki.example.org/wiki/Page").mock(side_effect=by_user_agent)
    body = (
        await client.post("/extract", json={"url": "https://wiki.example.org/wiki/Page"})
    ).json()
    assert body["title"] == "El Gobierno aprueba el nuevo plan de vivienda"
    assert route.call_count == 2
    assert route.calls.last.request.headers["user-agent"].startswith("search-extract-api/")


async def test_persistent_bot_block_is_access_blocked(client, net) -> None:
    net.get("https://wiki.example.org/wiki/Blocked").mock(
        return_value=httpx.Response(403, text=ROBOT_403, headers={"content-type": "text/html"})
    )
    response = await client.post("/extract", json={"url": "https://wiki.example.org/wiki/Blocked"})
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "ACCESS_BLOCKED"
    assert error["details"]["status_code"] == 403
