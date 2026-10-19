from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from ..core.security import Principal, require_principal
from ..schemas.extract import (
    BatchExtractRequest,
    BatchExtractResponse,
    ExtractRequest,
    ExtractResponse,
    StructuredRequest,
    StructuredResponse,
)

router = APIRouter(prefix="/extract", tags=["Extraction"])

_DESCRIPTION = (
    "Fetches a URL and returns its main content as clean Markdown, metadata (title, "
    "author, date, language, image) and a content fingerprint that does not change when "
    "only ads or page chrome change. Login walls, expired sessions and captchas are "
    "returned as errors (`LOGIN_REQUIRED`, `SESSION_EXPIRED`, `CAPTCHA_REQUIRED`), never "
    "as empty content."
)


@router.get("", response_model=ExtractResponse, summary="Extract a URL", description=_DESCRIPTION)
async def extract_get(
    request: Request,
    params: Annotated[ExtractRequest, Query()],
    principal: Principal = Depends(require_principal),
) -> ExtractResponse:
    return await request.app.state.extraction.extract(params, principal)


@router.post(
    "",
    response_model=ExtractResponse,
    summary="Extract a URL (JSON body)",
    description=_DESCRIPTION,
)
async def extract_post(
    request: Request, body: ExtractRequest, principal: Principal = Depends(require_principal)
) -> ExtractResponse:
    return await request.app.state.extraction.extract(body, principal)


@router.post("/batch", response_model=BatchExtractResponse, summary="Extract several URLs")
async def extract_batch(
    request: Request, body: BatchExtractRequest, principal: Principal = Depends(require_principal)
) -> BatchExtractResponse:
    """Each URL succeeds or fails independently (``items[].ok`` / ``items[].error``)."""
    return await request.app.state.extraction.extract_batch(body, principal)


@router.post("/structured", response_model=StructuredResponse, summary="Extract structured fields")
async def extract_structured(
    request: Request, body: StructuredRequest, principal: Principal = Depends(require_principal)
) -> StructuredResponse:
    """URL or text + a list of fields (or a JSON Schema) -> JSON with those fields,
    using Crawl4AI for the page and the configured LLM for the extraction. Meant for
    sites without a dedicated vertical."""
    return await request.app.state.ai.structured(body, principal)
