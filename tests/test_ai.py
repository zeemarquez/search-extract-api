"""A2 — AI endpoints: summarize, dedup, classify and verify (LLM mocked)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .conftest import LLM, fixture_text

pytestmark = pytest.mark.a2


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
    )


def last_prompt(route) -> str:
    payload = json.loads(route.calls.last.request.content)
    return payload["messages"][-1]["content"]


@pytest.fixture
async def ai_client(make_client):
    async with make_client(llm_base_url=LLM, llm_chat_model="test-model") as c:
        yield c


async def test_llm_not_configured(client) -> None:
    response = await client.post("/ai/summarize", json={"text": "hola"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_NOT_CONFIGURED"


async def test_llm_unreachable(ai_client, net) -> None:
    net.post(f"{LLM}/chat/completions").mock(side_effect=httpx.ConnectError("no"))
    response = await ai_client.post("/ai/summarize", json={"text": "hola"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_UNAVAILABLE"


async def test_summarize_text_language_and_length(ai_client, net) -> None:
    route = net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply(
            "<think>internal</think>El Gobierno aprobó un plan de vivienda de 3.500 millones."
        )
    )
    response = await ai_client.post(
        "/ai/summarize",
        json={"text": fixture_text("article_v1.html"), "language": "es", "length": "short"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "El Gobierno aprobó un plan de vivienda de 3.500 millones."
    assert body["language"] == "es"
    assert body["target_words"] == 60
    assert body["model"] == "test-model"
    prompt = last_prompt(route)
    assert "Spanish" in prompt and "60 words" in prompt
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "test-model"


async def test_summarize_group_of_results(ai_client, net) -> None:
    route = net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply("- Punto uno\n- Punto dos")
    )
    items = [
        {
            "title": "Huelga de metro el lunes",
            "snippet": "Los sindicatos convocan paros.",
            "source": "elpais.com",
        },
        {
            "title": "Paros en el metro de Madrid",
            "content": "Servicios mínimos del 50%.",
            "source": "elmundo.es",
        },
    ]
    body = (
        await ai_client.post(
            "/ai/summarize",
            json={"items": items, "format": "bullets", "language": "es", "max_words": 40},
        )
    ).json()
    assert body["input_items"] == 2
    assert body["summary"].startswith("- Punto")
    prompt = last_prompt(route)
    assert "[1] Huelga de metro el lunes (elpais.com)" in prompt
    assert "Servicios mínimos del 50%." in prompt
    assert "40 words" in prompt


async def test_summarize_long_text_map_reduce(make_client, net) -> None:
    calls: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["messages"][-1]["content"])
        return chat_reply(f"resumen {len(calls)}")

    net.post(f"{LLM}/chat/completions").mock(side_effect=reply)
    long_text = "\n".join(f"Párrafo {i}: " + "contenido relevante " * 40 for i in range(60))
    async with make_client(llm_base_url=LLM, llm_max_input_chars=5000) as c:
        body = (await c.post("/ai/summarize", json={"text": long_text})).json()
    assert body["chunks"] == 8 and body["truncated"] is True
    assert len(calls) == 9  # 8 partial summaries + 1 final
    assert "notes taken from a longer text" in calls[-1]


async def test_summarize_validation(ai_client) -> None:
    response = await ai_client.post("/ai/summarize", json={})
    assert response.status_code == 422


async def test_classify_with_labels(ai_client, net) -> None:
    route = net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply(
            '```json\n{"labels": [{"label": "economía", "confidence": 0.92}, {"label": "Deportes", "confidence": 0.1}]}\n```'
        )
    )
    body = (
        await ai_client.post(
            "/ai/classify",
            json={"text": "El Euríbor sube", "labels": ["Economía", "Deportes", "Política"]},
        )
    ).json()
    assert body["mode"] == "labels"
    assert body["results"][0]["labels"] == [{"label": "Economía", "confidence": 0.92}]
    assert '"Economía"' in last_prompt(route)


async def test_classify_multi_label_items(ai_client, net) -> None:
    net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply(
            '{"labels": [{"label": "Política", "confidence": 0.7}, {"label": "Economía", "confidence": 0.9}]}'
        )
    )
    body = (
        await ai_client.post(
            "/ai/classify",
            json={
                "items": [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}],
                "labels": ["Economía", "Política"],
                "multi_label": True,
            },
        )
    ).json()
    assert [r["id"] for r in body["results"]] == ["a", "b"]
    assert [lbl["label"] for lbl in body["results"][0]["labels"]] == ["Economía", "Política"]


async def test_classify_free_labels(ai_client, net) -> None:
    net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply(
            '{"labels": [{"label": "vivienda", "confidence": 95}, {"label": "Vivienda"}, {"label": "política"}]}'
        )
    )
    body = (
        await ai_client.post("/ai/classify", json={"text": "plan de vivienda", "max_labels": 2})
    ).json()
    assert body["mode"] == "free"
    labels = body["results"][0]["labels"]
    assert labels[0] == {"label": "vivienda", "confidence": 0.95}
    assert len(labels) == 2


async def test_invalid_llm_output_is_retried_then_reported(ai_client, net) -> None:
    route = net.post(f"{LLM}/chat/completions").mock(
        side_effect=[
            chat_reply("not json at all"),
            chat_reply('{"labels": [{"label": "Unknown"}]}'),
        ]
    )
    response = await ai_client.post("/ai/classify", json={"text": "x", "labels": ["A", "B"]})
    assert route.call_count == 2
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "LLM_BAD_OUTPUT"


async def test_retry_recovers(ai_client, net) -> None:
    net.post(f"{LLM}/chat/completions").mock(
        side_effect=[
            chat_reply("Sure! Here you go"),
            chat_reply('{"labels": [{"label": "B", "confidence": 0.8}]}'),
        ]
    )
    body = (await ai_client.post("/ai/classify", json={"text": "x", "labels": ["A", "B"]})).json()
    assert body["results"][0]["labels"][0]["label"] == "B"


async def test_verify_text_with_fields(ai_client, net) -> None:
    route = net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply(
            json.dumps(
                {
                    "matches": "true",
                    "confidence": 0.87,
                    "reason": "El texto anuncia una huelga de metro en Madrid el lunes.",
                    "fields": {
                        "city": "Madrid",
                        "date": "2026-10-20",
                        "lines_affected": "12",
                        "unknown": "x",
                    },
                }
            )
        )
    )
    body = (
        await ai_client.post(
            "/ai/verify",
            json={
                "criterion": "Anuncia una huelga de transporte en Madrid",
                "text": "Los sindicatos convocan huelga en las 12 líneas del metro de Madrid el lunes 20.",
                "language": "es",
                "fields": [
                    {"name": "city", "type": "string"},
                    {"name": "date", "type": "date"},
                    {"name": "lines_affected", "type": "integer"},
                ],
            },
        )
    ).json()
    assert body["matches"] is True
    assert body["confidence"] == 0.87
    assert body["fields"] == {"city": "Madrid", "date": "2026-10-20", "lines_affected": 12}
    prompt = last_prompt(route)
    assert "Criterion: Anuncia una huelga" in prompt and "Spanish" in prompt


async def test_verify_url_is_extracted_first(ai_client, net) -> None:
    net.get("https://www.diarioejemplo.es/economia/plan-vivienda").mock(
        return_value=httpx.Response(
            200, text=fixture_text("article_v1.html"), headers={"content-type": "text/html"}
        )
    )
    route = net.post(f"{LLM}/chat/completions").mock(
        return_value=chat_reply(
            '{"matches": false, "confidence": 0.9, "reason": "No menciona Barcelona.", "fields": {}}'
        )
    )
    body = (
        await ai_client.post(
            "/ai/verify",
            json={
                "criterion": "Habla de Barcelona",
                "url": "https://www.diarioejemplo.es/economia/plan-vivienda",
            },
        )
    ).json()
    assert body["matches"] is False
    assert body["url"] == "https://www.diarioejemplo.es/economia/plan-vivienda"
    assert "3.500 millones" in last_prompt(route)


async def test_dedup_lexical_without_embeddings(client) -> None:
    items: list[dict[str, Any]] = [
        {
            "id": "1",
            "title": "Huelga de metro en Madrid el lunes",
            "text": "Los sindicatos convocan huelga en el metro de Madrid",
        },
        {
            "id": "2",
            "title": "Convocada huelga en el metro de Madrid para el lunes",
            "text": "Los sindicatos del metro de Madrid convocan huelga",
        },
        {
            "id": "3",
            "title": "El Real Madrid gana la liga",
            "text": "El equipo blanco se proclama campeón de liga",
        },
        {
            "id": "4",
            "url": "https://elpais.com/x?utm_source=a",
            "title": "Otra cosa completamente distinta",
        },
        {"id": "5", "url": "https://www.elpais.com/x/", "title": "Titular cambiado"},
    ]
    body = (await client.post("/ai/dedup", json={"items": items})).json()
    assert body["method"] == "lexical"
    groups = sorted(sorted(c["items"]) for c in body["clusters"])
    assert ["1", "2"] in groups
    assert ["3"] in groups
    assert ["4", "5"] in groups  # same canonical URL
    assert body["duplicates"] == 2
    first = next(c for c in body["clusters"] if "1" in c["items"])
    assert first["representative"] == "1"


async def test_dedup_with_embeddings(make_client, net) -> None:
    vectors = {"a": [1.0, 0.0, 0.0], "b": [0.98, 0.05, 0.0], "c": [0.0, 1.0, 0.0]}

    def embed(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "embed-model"
        data = [
            {"index": i, "embedding": vectors[text.split("\n")[0]]}
            for i, text in enumerate(payload["input"])
        ]
        return httpx.Response(200, json={"data": data})

    route = net.post("http://embed.test/v1/embeddings").mock(side_effect=embed)
    async with make_client(
        llm_base_url=LLM, embeddings_base_url="http://embed.test/v1", embeddings_model="embed-model"
    ) as c:
        items = [{"id": "x", "title": "a"}, {"id": "y", "title": "b"}, {"id": "z", "title": "c"}]
        body = (await c.post("/ai/dedup", json={"items": items})).json()
        assert body["method"] == "embeddings"
        assert body["model"] == "embed-model"
        assert sorted(sorted(c["items"]) for c in body["clusters"]) == [["x", "y"], ["z"]]
        # Embeddings are cached: a second call does not hit the endpoint again.
        await c.post("/ai/dedup", json={"items": items})
        assert route.call_count == 1


async def test_dedup_auto_falls_back_to_lexical_when_embeddings_fail(make_client, net) -> None:
    net.post(f"{LLM}/embeddings").mock(return_value=httpx.Response(500))
    async with make_client(llm_base_url=LLM) as c:
        body = (
            await c.post(
                "/ai/dedup", json={"items": [{"title": "uno dos tres"}, {"title": "cuatro cinco"}]}
            )
        ).json()
    assert body["method"] == "lexical"
