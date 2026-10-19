"""Minimal client for OpenAI-compatible APIs (Ollama, vLLM, LM Studio, OpenAI...).

Chat and embeddings are configured separately (base URL, key and model), so a local
embeddings model can be combined with a hosted chat model or vice versa.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

import httpx

from ..config import Settings
from ..core.cache import Cache
from ..errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def extract_json(text: str) -> Any:
    """Parses JSON from an LLM reply, tolerating code fences, reasoning blocks and
    leading/trailing prose."""
    cleaned = _THINK.sub("", text or "").strip()
    fence = _FENCE.match(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except ValueError:
                continue
    raise ValueError("no JSON object found in the model output")


class LLMClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, cache: Cache) -> None:
        self.settings = settings
        self.client = client
        self.cache = cache
        self._semaphore = asyncio.Semaphore(settings.llm_concurrency)

    # ------------------------------------------------------------------ config
    @property
    def chat_configured(self) -> bool:
        return bool(self.settings.llm_base_url and self.settings.llm_chat_model)

    @property
    def embeddings_configured(self) -> bool:
        return bool(self.settings.effective_embeddings_base_url and self.settings.embeddings_model)

    @property
    def chat_model(self) -> str:
        return self.settings.llm_chat_model

    def _require_chat(self) -> None:
        if not self.chat_configured:
            raise ApiError(
                ErrorCode.LLM_NOT_CONFIGURED,
                "No LLM is configured. Set LLM_BASE_URL (any OpenAI-compatible endpoint, "
                "e.g. Ollama at http://localhost:11434/v1) and LLM_CHAT_MODEL.",
            )

    @staticmethod
    def _headers(api_key: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _post(self, url: str, payload: dict[str, Any], api_key: str | None) -> dict[str, Any]:
        try:
            async with self._semaphore:
                response = await self.client.post(
                    url,
                    json=payload,
                    headers=self._headers(api_key),
                    timeout=self.settings.llm_timeout,
                )
        except httpx.TimeoutException as exc:
            raise ApiError(ErrorCode.LLM_UNAVAILABLE, "The LLM endpoint timed out.") from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                ErrorCode.LLM_UNAVAILABLE,
                f"The LLM endpoint is unreachable ({type(exc).__name__}).",
            ) from exc
        if response.status_code >= 400:
            raise ApiError(
                ErrorCode.LLM_UNAVAILABLE,
                f"The LLM endpoint returned HTTP {response.status_code}.",
                details={"status_code": response.status_code, "body": response.text[:300]},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                ErrorCode.LLM_BAD_OUTPUT, "The LLM endpoint returned invalid JSON."
            ) from exc

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        use_cache: bool = True,
    ) -> str:
        self._require_chat()
        payload: dict[str, Any] = {
            "model": self.settings.llm_chat_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        cache_key = None
        if use_cache:
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            cache_key = f"llm:chat:{digest[:40]}"
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return cached.decode("utf-8")
        data = await self._post(
            f"{self.settings.llm_base_url}/chat/completions", payload, self.settings.llm_api_key
        )
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiError(ErrorCode.LLM_BAD_OUTPUT, "Unexpected chat completion format.") from exc
        content = _THINK.sub("", content).strip()
        if cache_key and content:
            await self.cache.set(cache_key, content.encode("utf-8"), self.settings.cache_ttl_ai)
        return content

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        validate: Any = None,
    ) -> Any:
        """Asks for a JSON answer; retries once with the error if parsing/validation fails."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error = ""
        for attempt in range(2):
            content = await self.chat(
                messages,
                json_mode=True,
                temperature=temperature,
                max_tokens=max_tokens,
                use_cache=attempt == 0,
            )
            try:
                data = extract_json(content)
                return validate(data) if validate else data
            except (ValueError, TypeError) as exc:
                last_error = str(exc)[:300]
                messages = [
                    *messages,
                    {"role": "assistant", "content": content[:4000]},
                    {
                        "role": "user",
                        "content": f"Your answer was not valid: {last_error}. Reply again with "
                        "only the corrected JSON object.",
                    },
                ]
        raise ApiError(
            ErrorCode.LLM_BAD_OUTPUT,
            "The model did not return valid structured output.",
            details={"error": last_error},
        )

    # ------------------------------------------------------------------ embeddings
    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.embeddings_configured:
            raise ApiError(
                ErrorCode.LLM_NOT_CONFIGURED,
                "No embeddings model is configured (EMBEDDINGS_BASE_URL / EMBEDDINGS_MODEL).",
            )
        model = self.settings.embeddings_model
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        keys: list[str] = []
        for index, text in enumerate(texts):
            key = "llm:emb:" + hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()[:40]
            keys.append(key)
            cached = await self.cache.get_json(key)
            if cached is not None:
                results[index] = cached
            else:
                missing.append(index)
        batch = max(1, self.settings.embeddings_batch_size)
        for start in range(0, len(missing), batch):
            chunk = missing[start : start + batch]
            data = await self._post(
                f"{self.settings.effective_embeddings_base_url}/embeddings",
                {"model": model, "input": [texts[i] for i in chunk]},
                self.settings.effective_embeddings_api_key,
            )
            try:
                items = sorted(data["data"], key=lambda item: item.get("index", 0))
                vectors = [item["embedding"] for item in items]
            except (KeyError, TypeError) as exc:
                raise ApiError(ErrorCode.LLM_BAD_OUTPUT, "Unexpected embeddings format.") from exc
            if len(vectors) != len(chunk):
                raise ApiError(ErrorCode.LLM_BAD_OUTPUT, "Embeddings count mismatch.")
            for index, vector in zip(chunk, vectors, strict=True):
                results[index] = vector
                await self.cache.set_json(keys[index], vector, self.settings.cache_ttl_embeddings)
        return [vector or [] for vector in results]

    async def ping(self) -> dict[str, Any]:
        """Cheap reachability probe used by ``/health?deep=true``."""
        if not self.chat_configured:
            return {"configured": False}
        try:
            response = await self.client.get(
                f"{self.settings.llm_base_url}/models",
                headers=self._headers(self.settings.llm_api_key),
                timeout=5,
            )
            return {"configured": True, "reachable": response.status_code < 500}
        except httpx.HTTPError:
            return {"configured": True, "reachable": False}
