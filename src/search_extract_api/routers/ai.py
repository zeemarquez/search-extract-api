from fastapi import APIRouter, Depends, Request

from ..core.security import Principal, require_principal
from ..schemas.ai import (
    ClassifyRequest,
    ClassifyResponse,
    DedupRequest,
    DedupResponse,
    SummarizeRequest,
    SummarizeResponse,
    VerifyRequest,
    VerifyResponse,
)

router = APIRouter(prefix="/ai", tags=["AI"])


@router.post("/summarize", response_model=SummarizeResponse, summary="Summarize a text or results")
async def summarize(
    request: Request, body: SummarizeRequest, principal: Principal = Depends(require_principal)
) -> SummarizeResponse:
    return await request.app.state.ai.summarize(body)


@router.post(
    "/dedup", response_model=DedupResponse, summary="Group items that describe the same thing"
)
async def dedup(
    request: Request, body: DedupRequest, principal: Principal = Depends(require_principal)
) -> DedupResponse:
    """Uses embeddings + cosine similarity when an embeddings model is configured and
    falls back to lexical (TF-IDF) similarity otherwise."""
    return await request.app.state.ai.dedup(body)


@router.post(
    "/classify", response_model=ClassifyResponse, summary="Classify with given or free labels"
)
async def classify(
    request: Request, body: ClassifyRequest, principal: Principal = Depends(require_principal)
) -> ClassifyResponse:
    return await request.app.state.ai.classify(body)


@router.post(
    "/verify", response_model=VerifyResponse, summary="Does this text meet this criterion?"
)
async def verify(
    request: Request, body: VerifyRequest, principal: Principal = Depends(require_principal)
) -> VerifyResponse:
    """Verification mode of classification: returns ``matches`` (yes/no), a
    ``confidence`` and the requested ``fields`` extracted from the text. Accepts a text
    or a URL (extracted first, optionally with a session)."""
    return await request.app.state.ai.verify(body, principal)
