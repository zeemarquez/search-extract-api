"""AI endpoints: summarize, deduplicate, classify, verify and structured extraction."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import Settings
from ..core.lang import ISO_TO_NAME, detect_language, normalize_language
from ..core.numbers import parse_number
from ..core.security import Principal
from ..errors import ApiError, ErrorCode
from ..schemas.ai import (
    ClassifyRequest,
    ClassifyResponse,
    ClassifyResult,
    DedupCluster,
    DedupRequest,
    DedupResponse,
    LabelScore,
    SummarizeRequest,
    SummarizeResponse,
    TextItem,
    VerifyRequest,
    VerifyResponse,
)
from ..schemas.extract import ExtractRequest, FieldSpec, StructuredRequest, StructuredResponse
from .dedup import DEFAULT_THRESHOLDS, greedy_clusters, lexical_vectors, normalize_rows, url_groups
from .llm import LLMClient

if TYPE_CHECKING:
    from ..extraction.service import ExtractionService

logger = logging.getLogger(__name__)

TARGET_WORDS = {"short": 60, "medium": 150, "long": 350}
MAX_CHUNKS = 8


def _language_name(code: str | None) -> str | None:
    if not code:
        return None
    return ISO_TO_NAME.get(code, code).capitalize()


def _chunk(text: str, size: int) -> list[str]:
    """Splits text in chunks of about ``size`` characters at paragraph boundaries."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        while len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        if len(current) + len(paragraph) + 1 > size and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def _clamp(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1.0 and number <= 100.0:
        number /= 100.0
    return round(max(0.0, min(1.0, number)), 4)


def fields_schema(fields: list[FieldSpec]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for spec in fields:
        prop: dict[str, Any] = {"type": [spec.type if spec.type != "date" else "string", "null"]}
        if spec.type == "date":
            prop["format"] = "date"
        if spec.type == "array":
            prop["items"] = {}
        if spec.description:
            prop["description"] = spec.description
        properties[spec.name] = prop
    return {
        "type": "object",
        "properties": properties,
        "required": [spec.name for spec in fields],
    }


def _coerce(value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None
    try:
        if kind == "number":
            if isinstance(value, str):
                parsed = parse_number(value)
                return -parsed if parsed is not None and value.strip().startswith("-") else parsed
            return float(value)
        if kind == "integer":
            if isinstance(value, str):
                parsed = parse_number(value)
                return int(parsed) if parsed is not None else None
            return int(float(value))
        if kind == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in {"true", "yes", "sí", "si", "1", "y"}
            return bool(value)
        if kind == "array" and not isinstance(value, list):
            return [value]
        if kind in {"string", "date"} and not isinstance(value, str):
            return (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, dict | list)
                else str(value)
            )
    except (TypeError, ValueError):
        return None
    return value


class AIService:
    def __init__(
        self, settings: Settings, llm: LLMClient, extraction: ExtractionService | None
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.extraction = extraction

    # ------------------------------------------------------------------ helpers
    async def _source_text(
        self,
        principal: Principal,
        *,
        text: str | None,
        url: str | None,
        session_id: str | None,
        mode: str | None,
        content: str = "main",
    ) -> tuple[str, str | None, str | None, str | None]:
        """Returns (text, url, title, fetcher)."""
        if text:
            return text, None, None, None
        if self.extraction is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, "Extraction is not available.")
        extracted = await self.extraction.extract(
            ExtractRequest(url=url or "", session_id=session_id, mode=mode, content=content),  # type: ignore[arg-type]
            principal,
        )
        return extracted.markdown, extracted.url, extracted.title, extracted.fetcher

    def _budget(self, text: str) -> tuple[str, bool]:
        limit = self.settings.llm_max_input_chars
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    # ------------------------------------------------------------------ summarize
    async def summarize(self, req: SummarizeRequest) -> SummarizeResponse:
        target = req.max_words or TARGET_WORDS[req.length]
        if req.items:
            per_item = max(300, self.settings.llm_max_input_chars // max(1, len(req.items)))
            blocks = []
            for index, item in enumerate(req.items, start=1):
                meta = ", ".join(filter(None, [item.source, item.published_at]))
                header = f"[{index}] {item.title or ''}" + (f" ({meta})" if meta else "")
                body = item.as_text(per_item) if item.text else ""
                if item.title and body.startswith(item.title):
                    body = body[len(item.title) :].strip()
                blocks.append(f"{header}\n{body}".strip())
            source = "\n\n".join(blocks)
            kind = f"a group of {len(req.items)} related texts/results"
        else:
            source = req.text or ""
            kind = "a text"
        language = normalize_language(req.language) or detect_language(source[:5000])
        language_name = _language_name(language) or "the same language as the input"
        style = (
            "Use concise bullet points (one fact per bullet, '- ' prefix)."
            if req.format == "bullets"
            else "Write flowing prose in one or a few short paragraphs."
        )
        focus = f"Focus on: {req.focus}\n" if req.focus else ""

        budget = self.settings.llm_max_input_chars
        chunks = _chunk(source, budget)
        truncated = len(chunks) > MAX_CHUNKS
        chunks = chunks[:MAX_CHUNKS]
        system = (
            "You are an expert editor who writes faithful, neutral summaries. Never invent "
            "facts that are not in the input. Output only the summary, no preamble."
        )
        if len(chunks) > 1:
            partial_words = max(80, target)

            async def summarize_chunk(chunk: str) -> str:
                return await self.llm.chat(
                    [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": f"Extract the key facts of this part of {kind} in about "
                            f"{partial_words} words, in {language_name}.\n{focus}\n<<<\n{chunk}\n>>>",
                        },
                    ]
                )

            partials = await asyncio.gather(*(summarize_chunk(c) for c in chunks))
            source = "\n\n".join(partials)
            kind = "notes taken from a longer text"
        summary = await self.llm.chat(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"Summarize {kind} in {language_name}, in at most {target} words. "
                    f"{style}\n{focus}\n<<<\n{source}\n>>>",
                },
            ]
        )
        return SummarizeResponse(
            summary=summary.strip(),
            language=language,
            format=req.format,
            target_words=target,
            model=self.llm.chat_model,
            input_items=len(req.items or []) or 1,
            input_chars=len(req.text or "")
            if req.text
            else sum(len(i.as_text()) for i in req.items or []),
            chunks=len(chunks),
            truncated=truncated,
        )

    # ------------------------------------------------------------------ dedup
    async def dedup(self, req: DedupRequest) -> DedupResponse:
        ids = [item.id or str(index) for index, item in enumerate(req.items)]
        if len(set(ids)) != len(ids):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Item ids must be unique.")
        texts = [item.as_text(1500) or (item.url or "") for item in req.items]
        method = req.method
        if method == "auto":
            method = "embeddings" if self.llm.embeddings_configured else "lexical"
        model: str | None = None
        vectors: np.ndarray
        if method == "embeddings":
            try:
                raw = await self.llm.embed(texts)
                vectors = normalize_rows(np.asarray(raw, dtype=np.float32))
                model = self.settings.embeddings_model
            except ApiError as exc:
                if req.method == "embeddings":
                    raise
                logger.warning("Embeddings unavailable (%s); using lexical dedup.", exc.code)
                method = "lexical"
        if method == "lexical":
            vectors = lexical_vectors(texts)
        threshold = req.threshold if req.threshold is not None else DEFAULT_THRESHOLDS[method]
        clusters = greedy_clusters(vectors, threshold, url_groups([item.url for item in req.items]))
        out = [
            DedupCluster(
                id=f"c{number}",
                representative=ids[members[0]],
                items=[ids[i] for i in members],
                size=len(members),
            )
            for number, members in enumerate(clusters, start=1)
        ]
        return DedupResponse(
            clusters=out,
            duplicates=len(req.items) - len(out),
            method=method,  # type: ignore[arg-type]
            threshold=threshold,
            model=model,
        )

    # ------------------------------------------------------------------ classify
    async def _classify_one(self, text: str, req: ClassifyRequest) -> list[LabelScore]:
        text, _ = self._budget(text)
        extra = f"\nAdditional instructions: {req.instructions}" if req.instructions else ""
        if req.labels:
            allowed = {label.casefold(): label for label in req.labels}
            how_many = (
                f"one or more (at most {req.max_labels}) of the allowed labels that apply"
                if req.multi_label
                else "exactly one allowed label, the best fit"
            )
            user = (
                f"Allowed labels: {json.dumps(req.labels, ensure_ascii=False)}\n"
                f"Choose {how_many}.{extra}\n"
                'Answer with JSON: {"labels": [{"label": "<allowed label>", "confidence": <0..1>}]}\n'
                f"Text:\n<<<\n{text}\n>>>"
            )

            def validate(data: Any) -> list[LabelScore]:
                items = data.get("labels") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    raise ValueError("'labels' must be a list")
                scores: list[LabelScore] = []
                for item in items:
                    name = item.get("label") if isinstance(item, dict) else item
                    key = str(name or "").strip().casefold()
                    if key in allowed and all(s.label != allowed[key] for s in scores):
                        confidence = item.get("confidence") if isinstance(item, dict) else None
                        scores.append(LabelScore(label=allowed[key], confidence=_clamp(confidence)))
                if not scores:
                    raise ValueError("no allowed label was returned")
                scores.sort(key=lambda s: s.confidence, reverse=True)
                return scores if req.multi_label else scores[:1]
        else:
            language = (
                _language_name(normalize_language(req.language)) or "the language of the text"
            )
            user = (
                f"Assign up to {req.max_labels} short topical labels (1-3 words each, in "
                f"{language}) that describe the text.{extra}\n"
                'Answer with JSON: {"labels": [{"label": "<label>", "confidence": <0..1>}]}\n'
                f"Text:\n<<<\n{text}\n>>>"
            )

            def validate(data: Any) -> list[LabelScore]:
                items = data.get("labels") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    raise ValueError("'labels' must be a list")
                scores: list[LabelScore] = []
                seen: set[str] = set()
                for item in items:
                    name = item.get("label") if isinstance(item, dict) else item
                    label = str(name or "").strip()[:80]
                    if label and label.casefold() not in seen:
                        seen.add(label.casefold())
                        confidence = item.get("confidence") if isinstance(item, dict) else None
                        scores.append(LabelScore(label=label, confidence=_clamp(confidence)))
                if not scores:
                    raise ValueError("no labels returned")
                scores.sort(key=lambda s: s.confidence, reverse=True)
                return scores[: req.max_labels]

        return await self.llm.chat_json(
            "You are a precise text classifier. Answer only with JSON.", user, validate=validate
        )

    async def classify(self, req: ClassifyRequest) -> ClassifyResponse:
        items = req.items or [TextItem(text=req.text)]
        results = await asyncio.gather(*(self._classify_one(item.as_text(), req) for item in items))
        return ClassifyResponse(
            results=[
                ClassifyResult(id=item.id, labels=labels)
                for item, labels in zip(items, results, strict=True)
            ],
            mode="labels" if req.labels else "free",
            model=self.llm.chat_model,
        )

    # ------------------------------------------------------------------ verify
    async def verify(self, req: VerifyRequest, principal: Principal) -> VerifyResponse:
        source, url, _, _ = await self._source_text(
            principal, text=req.text, url=req.url, session_id=req.session_id, mode=req.mode
        )
        text, truncated = self._budget(source)
        language = _language_name(normalize_language(req.language)) or "the language of the text"
        fields = req.fields or []
        fields_part = ""
        if fields:
            fields_part = (
                "Also extract these fields from the text (null when absent, never invented): "
                f"{json.dumps(fields_schema(fields)['properties'], ensure_ascii=False)}\n"
            )
        user = (
            f"Criterion: {req.criterion}\n{fields_part}"
            "Decide whether the text clearly satisfies the criterion. Be strict: if the text "
            "does not support it explicitly, the answer is false.\n"
            'Answer with JSON: {"matches": true|false, "confidence": <0..1>, '
            f'"reason": "<one or two sentences in {language}>", "fields": {{...}}}}\n'
            f"Text:\n<<<\n{text}\n>>>"
        )

        def validate(data: Any) -> dict[str, Any]:
            if not isinstance(data, dict) or "matches" not in data:
                raise ValueError("expected an object with 'matches'")
            matches = data["matches"]
            if isinstance(matches, str):
                matches = matches.strip().lower() in {"true", "yes", "sí", "si"}
            raw_fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
            return {
                "matches": bool(matches),
                "confidence": _clamp(data.get("confidence"), 0.5),
                "reason": str(data.get("reason") or "").strip(),
                "fields": {
                    spec.name: _coerce(raw_fields.get(spec.name), spec.type) for spec in fields
                },
            }

        result = await self.llm.chat_json(
            "You verify whether a text satisfies a criterion. Answer only with JSON.",
            user,
            validate=validate,
        )
        return VerifyResponse(
            **result,
            model=self.llm.chat_model,
            url=url,
            source_chars=len(source),
            truncated=truncated,
        )

    # ------------------------------------------------------------------ structured
    async def structured(self, req: StructuredRequest, principal: Principal) -> StructuredResponse:
        self.llm._require_chat()
        source, url, title, fetcher = await self._source_text(
            principal,
            text=req.text,
            url=req.url,
            session_id=req.session_id,
            mode=req.mode,
            content="full",
        )
        text, truncated = self._budget(source)
        if req.fields:
            schema = fields_schema(req.fields)
            kinds = {spec.name: spec.type for spec in req.fields}
            required = [spec.name for spec in req.fields if spec.required]
        else:
            schema = req.json_schema or {}
            props = schema.get("properties") or {}
            kinds = {
                name: (spec.get("type") if isinstance(spec.get("type"), str) else "string")
                for name, spec in props.items()
                if isinstance(spec, dict)
            }
            required = [name for name in schema.get("required") or [] if isinstance(name, str)]
        extra = f"\nAdditional instructions: {req.instructions}" if req.instructions else ""
        user = (
            "Extract the data described by this JSON Schema from the content below. Use null "
            "for values that are not present; never invent values. Keep the original "
            f"language of the content for text values.{extra}\n"
            f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}\n"
            f"Content{f' (from {url})' if url else ''}:\n<<<\n{text}\n>>>"
        )

        def validate(data: Any) -> dict[str, Any]:
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            if not kinds:
                return data
            return {name: _coerce(data.get(name), kind) for name, kind in kinds.items()}

        data = await self.llm.chat_json(
            "You extract structured data from web content. Answer only with JSON.",
            user,
            validate=validate,
        )
        missing = [name for name in required if data.get(name) in (None, "", [])]
        return StructuredResponse(
            data=data,
            missing_fields=missing,
            url=url,
            title=title,
            model=self.llm.chat_model,
            source_chars=len(source),
            truncated=truncated,
            fetcher=fetcher,
        )
