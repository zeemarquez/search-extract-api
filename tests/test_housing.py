"""A4 — housing vertical: Idealista website/API and filters."""

from __future__ import annotations

import base64

import httpx
import pytest

from search_extract_api.verticals.housing.schemas import HousingResult
from search_extract_api.verticals.housing.sources import (
    idealista_location_slug,
    parse_idealista_listing,
)

from .conftest import SEARXNG, fixture_text, searxng_item, searxng_payload

pytestmark = pytest.mark.a4

LISTING_URL = "https://www.idealista.com/venta-viviendas/madrid-madrid/"
DATADOME = (
    "<html><head><title>idealista.com</title></head><body>"
    "<script>var dd={'rt':'c','cid':'AHrlqAAAAAMA','hsh':'abc','t':'fe','host':'geo.captcha-delivery.com'}</script>"
    "<script src='https://ct.captcha-delivery.com/c.js'></script></body></html>"
)


def test_parse_idealista_listing() -> None:
    results = parse_idealista_listing(fixture_text("idealista_listing.html"), "sale", "homes")
    assert len(results) == 2
    first = results[0]
    assert first.url == "https://www.idealista.com/inmueble/104512345/"
    assert first.price == 450000.0 and first.currency == "EUR"
    assert first.rooms == 3 and first.area_m2 == 95.0
    assert first.location == "calle de Alcalá, Goya, Madrid"
    assert first.operation == "sale"
    assert first.image.startswith("https://img4.idealista.com/")
    second = results[1]
    assert second.bathrooms == 2 and second.price == 820000.0


@pytest.mark.parametrize(
    ("location", "slug"),
    [
        ("Madrid", "madrid-madrid"),
        ("Málaga", "malaga-malaga"),
        ("chamberi-madrid", "chamberi-madrid"),
    ],
)
def test_location_slug(location, slug) -> None:
    assert idealista_location_slug(location) == slug


async def test_housing_search_with_filters(client, net) -> None:
    route = net.get(LISTING_URL).mock(
        return_value=httpx.Response(
            200, text=fixture_text("idealista_listing.html"), headers={"content-type": "text/html"}
        )
    )
    body = (
        await client.get(
            "/housing/search",
            params={"operation": "sale", "location": "Madrid", "max_price": 500000, "min_rooms": 3},
        )
    ).json()
    assert route.called
    assert body["failed_sources"] == []
    assert [r["url"] for r in body["results"]] == ["https://www.idealista.com/inmueble/104512345/"]
    result = HousingResult.model_validate(body["results"][0])
    assert result.price == 450000.0 and result.area_m2 == 95.0
    statuses = {s["name"]: s for s in body["sources"]}
    assert statuses["idealista_api"]["skipped"] is True  # no API keys configured


async def test_rent_rooms_path_and_text_filter(client, net) -> None:
    route = net.get("https://www.idealista.com/alquiler-habitacion/barcelona-barcelona/").mock(
        return_value=httpx.Response(
            200, text=fixture_text("idealista_listing.html"), headers={"content-type": "text/html"}
        )
    )
    body = (
        await client.get(
            "/housing/search",
            params={
                "operation": "rent",
                "property_type": "rooms",
                "location": "Barcelona",
                "q": "ático",
            },
        )
    ).json()
    assert route.called
    assert [r["location"] for r in body["results"]] == ["calle de Fuencarral, Malasaña, Madrid"]


async def test_captcha_is_reported_not_empty(client, net) -> None:
    net.get(LISTING_URL).mock(
        return_value=httpx.Response(403, text=DATADOME, headers={"content-type": "text/html"})
    )
    body = (await client.get("/housing/search", params={"location": "madrid"})).json()
    assert body["failed_sources"] == ["idealista"]
    status = body["sources"][0] if body["sources"][0]["name"] == "idealista" else body["sources"][1]
    assert status["error"]["code"] == "CAPTCHA_REQUIRED"


async def test_location_is_required(client) -> None:
    response = await client.get("/housing/search", params={"operation": "sale"})
    assert response.status_code == 422


async def test_idealista_official_api(make_client, net) -> None:
    token = net.post("https://api.idealista.com/oauth/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok", "token_type": "bearer", "expires_in": 43199}
        )
    )
    search = net.post("https://api.idealista.com/3.5/es/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "elementList": [
                    {
                        "propertyCode": "999",
                        "price": 1200.0,
                        "size": 60.0,
                        "rooms": 2,
                        "bathrooms": 1,
                        "address": "Calle Mayor",
                        "district": "Centro",
                        "municipality": "Madrid",
                        "operation": "rent",
                        "propertyType": "flat",
                        "url": "https://www.idealista.com/inmueble/999/",
                        "thumbnail": "https://img.idealista.com/999.jpg",
                        "latitude": 40.41,
                        "longitude": -3.7,
                        "suggestedTexts": {"title": "Piso en Calle Mayor"},
                    }
                ],
                "total": 1,
            },
        )
    )
    async with make_client(idealista_api_key="k", idealista_api_secret="s") as c:
        params = {
            "operation": "rent",
            "latitude": 40.41,
            "longitude": -3.70,
            "radius_m": 1500,
            "min_rooms": 2,
            "sources": "idealista_api",
        }
        body = (await c.get("/housing/search", params=params)).json()
        await c.get("/housing/search", params={**params, "no_cache": "true"})
    assert body["failed_sources"] == []
    result = body["results"][0]
    assert result["price"] == 1200.0 and result["rooms"] == 2 and result["operation"] == "rent"
    assert result["location"] == "Calle Mayor, Centro, Madrid"
    assert token.call_count == 1  # token cached
    auth = token.calls.last.request.headers["authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == "k:s"
    form = dict(httpx.QueryParams(search.calls.last.request.content.decode()))
    assert form["center"] == "40.41,-3.7" and form["distance"] == "1500"
    assert form["bedrooms"] == "2,3,4"
    assert search.calls.last.request.headers["authorization"] == "Bearer tok"


async def test_web_housing_source_is_opt_in(client, net) -> None:
    net.get(f"{SEARXNG}/search").mock(
        return_value=httpx.Response(
            200,
            json=searxng_payload(
                [
                    searxng_item(
                        "https://www.fotocasa.es/es/comprar/vivienda/madrid/123",
                        "Piso en venta en Madrid - Fotocasa",
                        "Piso de 3 habitaciones y 80 m² por 320.000 €",
                    ),
                ]
            ),
        )
    )
    body = (
        await client.get("/housing/search", params={"location": "madrid", "sources": "web_housing"})
    ).json()
    result = HousingResult.model_validate(body["results"][0])
    assert result.price == 320000.0 and result.rooms == 3 and result.area_m2 == 80.0
    assert result.operation == "sale" and result.location == "madrid"
