"""Builds the HTTP endpoints of a vertical from its declared query/result models.

Deliberately *not* using ``from __future__ import annotations``: the endpoint
signatures below must hold the real model classes for FastAPI to introspect them.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from ..adapters.base import SourceInfo
from ..core.security import Principal, require_principal
from ..schemas.common import BaseQuery, SearchResponse
from .base import Vertical


def build_vertical_router(vertical: Vertical) -> APIRouter:
    query_model = vertical.query_model
    response_model = SearchResponse[vertical.result_model]  # type: ignore[name-defined]
    tag = vertical.title or vertical.name
    router = APIRouter(prefix=vertical.prefix, tags=[tag])

    async def search_get(
        request: Request,
        query: Annotated[query_model, Query()],  # type: ignore[valid-type]
        principal: Principal = Depends(require_principal),
    ) -> Any:
        return await request.app.state.search.search(vertical.name, query, principal)

    async def search_post(
        request: Request,
        query: query_model,  # type: ignore[valid-type]
        principal: Principal = Depends(require_principal),
    ) -> Any:
        return await request.app.state.search.search(vertical.name, query, principal)

    async def list_sources(
        request: Request, principal: Principal = Depends(require_principal)
    ) -> list[SourceInfo]:
        registry = request.app.state.registry
        settings = request.app.state.settings
        return [source.info(settings) for source in registry.sources_for(vertical.name)]

    summary = f"{tag}: search"
    description = (
        f"{vertical.description}\n\nResults from all sources are merged and deduplicated by "
        "URL. A failing source never fails the request: see `sources` and `failed_sources`."
    )
    router.add_api_route(
        vertical.search_path,
        search_get,
        methods=["GET"],
        response_model=response_model,
        summary=summary,
        description=description,
        operation_id=f"{vertical.name}_search",
    )
    router.add_api_route(
        vertical.search_path,
        search_post,
        methods=["POST"],
        response_model=response_model,
        summary=f"{summary} (JSON body)",
        description=description,
        operation_id=f"{vertical.name}_search_post",
    )
    router.add_api_route(
        vertical.sources_path,
        list_sources,
        methods=["GET"],
        response_model=list[SourceInfo],
        summary=f"{tag}: available sources",
        operation_id=f"{vertical.name}_sources",
    )
    vertical.extra_routes(router)
    return router


__all__ = ["BaseQuery", "build_vertical_router"]
