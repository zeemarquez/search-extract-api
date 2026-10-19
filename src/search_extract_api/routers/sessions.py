import asyncio
import contextlib
import hashlib
import logging
import secrets
from importlib import resources

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from ..core.cookies import parse_cookies
from ..core.security import Principal, require_principal
from ..errors import ApiError, ErrorCode
from ..sessions.models import (
    LoginInfo,
    LoginStartRequest,
    SessionCheckResult,
    SessionImportRequest,
    SessionInfo,
    SessionUpdateRequest,
)
from ..sessions.profiles import registered_profiles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["Sessions"])

_RISK_NOTE = (
    "Automated access with a personal account may break some sites' terms of service and "
    "can get the account restricted. Use it at your own risk."
)


# --------------------------------------------------------------------------- login
@router.post(
    "/login",
    response_model=LoginInfo,
    status_code=201,
    summary="Start an interactive login",
    description="Opens a remote headless browser on the site's login page and returns a "
    "`viewer_url` for the user to log in. Only the resulting session state is stored, "
    "never the password. " + _RISK_NOTE,
)
async def login_start(
    request: Request, body: LoginStartRequest, principal: Principal = Depends(require_principal)
) -> LoginInfo:
    return await request.app.state.logins.start(principal, body, str(request.base_url))


@router.get("/login/{login_id}", response_model=LoginInfo, summary="Interactive login status")
async def login_status(
    request: Request, login_id: str, principal: Principal = Depends(require_principal)
) -> LoginInfo:
    return request.app.state.logins.get(principal, login_id)


@router.post(
    "/login/{login_id}/complete", response_model=LoginInfo, summary="Save the logged-in session"
)
async def login_complete(
    request: Request, login_id: str, principal: Principal = Depends(require_principal)
) -> LoginInfo:
    return await request.app.state.logins.complete_owned(principal, login_id)


@router.delete("/login/{login_id}", response_model=LoginInfo, summary="Cancel an interactive login")
async def login_cancel(
    request: Request, login_id: str, principal: Principal = Depends(require_principal)
) -> LoginInfo:
    return await request.app.state.logins.cancel_owned(principal, login_id)


@router.get("/login/{login_id}/view", response_class=HTMLResponse, include_in_schema=False)
async def login_viewer(login_id: str) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    page = resources.files("search_extract_api.sessions").joinpath("viewer.html").read_text("utf-8")
    page = page.replace("<script>", f'<script nonce="{nonce}">').replace(
        "<style>", f'<style nonce="{nonce}">'
    )
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'"
            ),
        },
    )


@router.websocket("/login/{login_id}/ws")
async def login_socket(websocket: WebSocket, login_id: str) -> None:
    manager = websocket.app.state.logins
    interval = websocket.app.state.settings.login_frame_interval
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if not isinstance(first, dict) or first.get("type") != "auth":
            raise ApiError(ErrorCode.LOGIN_NOT_FOUND, "Authenticate first.")
        state = manager.authorize_viewer(login_id, str(first.get("token", "")))
    except ApiError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "message": exc.message})
            await websocket.close(code=4404)
        return
    except Exception:  # timeout, disconnect, binary or non-JSON first message
        with contextlib.suppress(Exception):
            await websocket.close(code=4401)
        return

    send_lock = asyncio.Lock()

    async def send_json(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def sender() -> None:
        last_frame: str | None = None
        last_status: dict | None = None
        while state.status == "pending":
            frame = await manager.screenshot(state)
            if frame:
                digest = hashlib.blake2b(frame, digest_size=16).hexdigest()
                if digest != last_frame:
                    async with send_lock:
                        await websocket.send_bytes(frame)
                    last_frame = digest
            status = await manager.page_status(state)
            if status != last_status:
                await send_json(status)
                last_status = status
            await asyncio.sleep(interval)
        closing: dict = {"type": "closed", "status": state.status}
        if state.session is not None:
            closing["session"] = state.session.model_dump(mode="json")
        await send_json(closing)

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            event = await websocket.receive_json()
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "done":
                try:
                    info = await manager.complete(state)
                    await send_json(
                        {
                            "type": "completed",
                            "session": info.session.model_dump(mode="json")
                            if info.session
                            else None,
                        }
                    )
                except ApiError as exc:
                    await send_json({"type": "error", "message": exc.message})
            elif kind == "cancel":
                await manager.cancel(state)
            else:
                await manager.handle_event(state, event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except ValueError:  # non-JSON message
        pass
    finally:
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender_task


# --------------------------------------------------------------------------- sessions
@router.post(
    "",
    response_model=SessionInfo,
    status_code=201,
    summary="Import a session from cookies",
    description="Accepts cookies exported from a browser (JSON from Cookie-Editor / "
    "EditThisCookie, a Playwright storage state, or a Netscape cookies.txt). Cookies are "
    "filtered to the given domain, encrypted at rest and never returned. " + _RISK_NOTE,
)
async def import_session(
    request: Request, body: SessionImportRequest, principal: Principal = Depends(require_principal)
) -> SessionInfo:
    service = request.app.state.sessions
    info = await service.import_cookies(
        principal, body.domain, body.cookies, body.format, body.label
    )
    if body.check:
        info = (await service.check(principal, info.id)).session
    return info


@router.get("", response_model=list[SessionInfo], summary="List your sessions")
async def list_sessions(
    request: Request, principal: Principal = Depends(require_principal)
) -> list[SessionInfo]:
    return await request.app.state.sessions.list(principal)


@router.get("/sites", summary="Sites with built-in session support")
async def session_sites(principal: Principal = Depends(require_principal)) -> list[dict]:
    return [
        {
            "domain": p.domain,
            "login_url": p.login_url,
            "check_url": p.check_url,
            "auth_cookies": list(p.auth_cookies),
            "notes": list(p.notes),
        }
        for p in registered_profiles()
    ]


@router.get("/{session_id}", response_model=SessionInfo, summary="Get a session (metadata only)")
async def get_session(
    request: Request, session_id: str, principal: Principal = Depends(require_principal)
) -> SessionInfo:
    return await request.app.state.sessions.get(principal, session_id)


@router.patch("/{session_id}", response_model=SessionInfo, summary="Replace cookies or label")
async def update_session(
    request: Request,
    session_id: str,
    body: SessionUpdateRequest,
    principal: Principal = Depends(require_principal),
) -> SessionInfo:
    state = parse_cookies(body.cookies, body.format) if body.cookies is not None else None
    return await request.app.state.sessions.replace_state(
        principal, session_id, state=state, label=body.label
    )


@router.delete("/{session_id}", status_code=204, summary="Delete a session")
async def delete_session(
    request: Request, session_id: str, principal: Principal = Depends(require_principal)
) -> Response:
    await request.app.state.sessions.delete(principal, session_id)
    return Response(status_code=204)


@router.post(
    "/{session_id}/check", response_model=SessionCheckResult, summary="Check a session's health"
)
async def check_session(
    request: Request, session_id: str, principal: Principal = Depends(require_principal)
) -> SessionCheckResult:
    """Loads a logged-in-only page of the site with the session and reports ``active``,
    ``expired`` (login wall / expired cookies), ``captcha`` or ``error``."""
    return await request.app.state.sessions.check(principal, session_id)
