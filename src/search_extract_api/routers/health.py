import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request

from .. import __version__
from ..core.security import Principal, require_principal

router = APIRouter(tags=["Service"])


async def _probe(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    try:
        response = await client.get(url, headers=headers or {}, timeout=5, follow_redirects=True)
        return {"reachable": response.status_code < 500, "status_code": response.status_code}
    except httpx.HTTPError as exc:
        return {"reachable": False, "error": type(exc).__name__}


@router.get("/health", summary="Health check")
async def health(
    request: Request, deep: bool = Query(False, description="Probe dependencies")
) -> dict[str, Any]:
    """Shallow by default (for container health checks). ``deep=true`` also probes
    SearXNG, Crawl4AI and the LLM endpoint. The service reports ``degraded`` (never
    fails) when optional dependencies are missing."""
    state = request.app.state
    settings = state.settings
    components: dict[str, Any] = {
        "cache": {"backend": state.cache.backend_name, "ok": not state.cache.degraded},
        "sessions_store": {"ok": True},
        "browser": state.browser.status,
        "searxng": {"configured": bool(settings.searxng_url)},
        "crawl4ai": {"configured": bool(settings.crawl4ai_url)},
        "llm": {
            "configured": state.llm.chat_configured,
            "chat_model": settings.llm_chat_model if state.llm.chat_configured else None,
            "embeddings_configured": state.llm.embeddings_configured,
        },
    }
    try:
        await state.sessions.store.count()
    except Exception as exc:  # pragma: no cover - disk issues
        components["sessions_store"] = {"ok": False, "error": type(exc).__name__}
    if deep:
        client: httpx.AsyncClient = state.http.client
        probes: dict[str, Any] = {}
        if settings.searxng_url:
            probes["searxng"] = _probe(client, f"{settings.searxng_url}/healthz")
        if settings.crawl4ai_url:
            probes["crawl4ai"] = _probe(client, f"{settings.crawl4ai_url}/health")
        names = list(probes)
        results = await asyncio.gather(*probes.values())
        for name, result in zip(names, results, strict=True):
            components[name].update(result)
        components["llm"].update(await state.llm.ping())
        components["cache"]["ok"] = await state.cache.ping()
    degraded = (
        state.cache.degraded
        or not components["sessions_store"]["ok"]
        or any(
            comp.get("configured") and comp.get("reachable") is False
            for comp in components.values()
            if isinstance(comp, dict)
        )
    )
    return {
        "status": "degraded" if degraded else "ok",
        "version": __version__,
        "components": components,
    }


@router.get("/verticals", summary="List verticals, their sources and schemas")
async def verticals(
    request: Request, principal: Principal = Depends(require_principal)
) -> list[dict[str, Any]]:
    registry = request.app.state.registry
    settings = request.app.state.settings
    return [
        vertical.describe(registry.sources_for(name), settings)
        for name, vertical in registry.verticals.items()
    ]
