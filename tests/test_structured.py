"""A3 — structured extraction (page or text + fields -> JSON)."""

from __future__ import annotations

import json

import httpx
import pytest

from .conftest import LLM, fixture_text

pytestmark = pytest.mark.a3


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


@pytest.fixture
async def ai_client(make_client):
    async with make_client(llm_base_url=LLM, llm_chat_model="test-model") as c:
        yield c


async def test_structured_from_text_with_fields(ai_client, net) -> None:
    route = net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply(
            json.dumps(
                {
                    "price": "1.250,50",
                    "rooms": "3",
                    "furnished": "yes",
                    "address": None,
                    "tags": "terrace",
                }
            )
        )
    )
    body = (
        await ai_client.post(
            "/extract/structured",
            json={
                "text": "Piso amueblado de 3 habitaciones con terraza por 1.250,50 € al mes.",
                "fields": [
                    {"name": "price", "type": "number", "description": "Monthly price"},
                    {"name": "rooms", "type": "integer"},
                    {"name": "furnished", "type": "boolean"},
                    {"name": "address", "type": "string", "required": True},
                    {"name": "tags", "type": "array"},
                ],
            },
        )
    ).json()
    assert body["data"] == {
        "price": 1250.5,
        "rooms": 3,
        "furnished": True,
        "address": None,
        "tags": ["terrace"],
    }
    assert body["missing_fields"] == ["address"]
    assert body["model"] == "test-model"
    payload = json.loads(route.calls.last.request.content)
    assert payload["response_format"] == {"type": "json_object"}
    assert '"rooms"' in payload["messages"][-1]["content"]


async def test_structured_from_url_uses_full_page(ai_client, net) -> None:
    url = "https://www.diarioejemplo.es/economia/plan-vivienda"
    net.get(url).mock(
        return_value=httpx.Response(
            200, text=fixture_text("article_v1.html"), headers={"content-type": "text/html"}
        )
    )
    route = net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply('{"amount_eur": 3500000000, "homes": 50000}')
    )
    body = (
        await ai_client.post(
            "/extract/structured",
            json={
                "url": url,
                "json_schema": {
                    "type": "object",
                    "properties": {"amount_eur": {"type": "number"}, "homes": {"type": "integer"}},
                    "required": ["amount_eur"],
                },
                "instructions": "Amounts in euros as plain numbers.",
            },
        )
    ).json()
    assert body["data"] == {"amount_eur": 3500000000.0, "homes": 50000}
    assert body["url"] == url
    assert body["title"] == "El Gobierno aprueba el nuevo plan de vivienda"
    assert body["fetcher"] == "light"
    prompt = json.loads(route.calls.last.request.content)["messages"][-1]["content"]
    assert "50.000 viviendas" in prompt and "Amounts in euros" in prompt


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "x"},
        {"text": "x", "url": "https://a.com", "fields": [{"name": "a"}]},
        {"fields": [{"name": "a"}]},
        {"text": "x", "fields": [{"name": "bad name"}]},
    ],
)
async def test_structured_validation(ai_client, payload) -> None:
    response = await ai_client.post("/extract/structured", json=payload)
    assert response.status_code == 422


async def test_structured_needs_llm(client) -> None:
    response = await client.post(
        "/extract/structured", json={"text": "x", "fields": [{"name": "a"}]}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_NOT_CONFIGURED"
