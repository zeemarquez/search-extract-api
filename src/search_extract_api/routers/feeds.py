from fastapi import APIRouter, Depends, Query, Request

from ..core.security import Principal, require_principal
from ..schemas.feeds import DiscoverResponse, FeedResponse, FeedsRequest, FeedsResponse

router = APIRouter(prefix="/feeds", tags=["Feeds"])


@router.get("", response_model=FeedResponse, summary="Read an RSS/Atom/JSON feed")
async def read_feed(
    request: Request,
    url: str = Query(..., description="Feed URL"),
    limit: int = Query(50, ge=1, le=500),
    session_id: str | None = Query(None, description="Session for feeds behind a login"),
    no_cache: bool = False,
    principal: Principal = Depends(require_principal),
) -> FeedResponse:
    return await request.app.state.feeds.read(
        url, principal=principal, limit=limit, session_id=session_id, no_cache=no_cache
    )


@router.post("", response_model=FeedsResponse, summary="Read several feeds at once")
async def read_feeds(
    request: Request, body: FeedsRequest, principal: Principal = Depends(require_principal)
) -> FeedsResponse:
    """Merges, deduplicates and sorts (newest first) the items of several feeds. A feed
    that fails is reported in ``failed_sources``; the others are still returned."""
    return await request.app.state.feeds.read_many(body, principal)


@router.get("/discover", response_model=DiscoverResponse, summary="Find the feeds of a website")
async def discover(
    request: Request,
    url: str = Query(..., description="Any page of the website"),
    verify: bool = Query(True, description="Fetch candidates to confirm they are feeds"),
    principal: Principal = Depends(require_principal),
) -> DiscoverResponse:
    return await request.app.state.feeds.discover(url, verify=verify)
