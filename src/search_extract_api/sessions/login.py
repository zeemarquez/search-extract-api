"""Interactive login through a remote-controlled headless browser.

Flow:

1. ``POST /sessions/login`` opens a browser context on the site's login page and
   returns a ``viewer_url`` (with a one-time secret in the URL *fragment*, so it never
   reaches server logs).
2. The user opens the viewer, sees the remote browser and types their credentials
   (and 2FA codes) there. Keystrokes are forwarded to the browser and never stored
   or logged.
3. When logged in, the user presses "Save session" (or the client calls
   ``POST /sessions/login/{id}/complete``): only the resulting storage state
   (cookies + localStorage) is encrypted and stored. The password never is.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from ..core.cookies import StorageState
from ..core.security import Principal
from ..core.urls import domain_matches, host_of, is_http_url
from ..errors import ApiError, ErrorCode
from .browser import BrowserManager
from .models import LoginInfo, LoginStartRequest, LoginStatus, SessionInfo
from .profiles import profile_for
from .service import SessionService

logger = logging.getLogger(__name__)

MAX_PENDING_LOGINS = 5
KEEP_FINISHED_SECONDS = 600
VIEWPORT = (1280, 800)
ALLOWED_KEYS = {
    "Enter",
    "Tab",
    "Backspace",
    "Delete",
    "Escape",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Space",
}


@dataclass
class LoginState:
    id: str
    token_hash: str
    owner: Principal
    domain: str
    label: str | None
    refresh_session_id: str | None
    created_at: float
    expires_at: float
    status: LoginStatus = "pending"
    context: Any = None
    page: Any = None
    session: SessionInfo | None = None
    error: str | None = None
    finished_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def info(self, viewer_url: str | None = None) -> LoginInfo:
        return LoginInfo(
            login_id=self.id,
            domain=self.domain,
            status=self.status,
            viewer_url=viewer_url,
            expires_at=datetime.fromtimestamp(self.expires_at, UTC),
            session=self.session,
            error=self.error,
        )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginManager:
    def __init__(self, settings: Settings, browser: BrowserManager, sessions: SessionService):
        self.settings = settings
        self.browser = browser
        self.sessions = sessions
        self._logins: dict[str, LoginState] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle
    def start_background(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        for state in list(self._logins.values()):
            await self._close_context(state)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self.expire_stale()

    async def expire_stale(self) -> None:
        now = time.time()
        for state in list(self._logins.values()):
            if state.status == "pending" and now > state.expires_at:
                state.status = "expired"
                state.finished_at = now
                await self._close_context(state)
            elif state.finished_at and now - state.finished_at > KEEP_FINISHED_SECONDS:
                self._logins.pop(state.id, None)

    @staticmethod
    async def _close_context(state: LoginState) -> None:
        if state.context is not None:
            with contextlib.suppress(Exception):
                await state.context.close()
        state.context = None
        state.page = None

    # ------------------------------------------------------------------ API
    async def start(self, principal: Principal, req: LoginStartRequest, base_url: str) -> LoginInfo:
        pending = [s for s in self._logins.values() if s.status == "pending"]
        if len(pending) >= MAX_PENDING_LOGINS:
            raise ApiError(
                ErrorCode.BROWSER_UNAVAILABLE,
                "Too many interactive logins in progress; try again in a few minutes.",
            )
        if req.session_id:
            existing = await self.sessions.get(principal, req.session_id)
            if existing.domain != req.domain:
                raise ApiError(
                    ErrorCode.SESSION_DOMAIN_MISMATCH,
                    f"Session {req.session_id} belongs to {existing.domain}.",
                )
        profile = profile_for(req.domain)
        login_url = req.login_url or profile.login_url or f"https://{req.domain}/login"
        if not is_http_url(login_url) or not domain_matches(host_of(login_url), req.domain):
            raise ApiError(
                ErrorCode.INVALID_URL, f"login_url must be an http(s) URL on {req.domain}."
            )

        context = await self.browser.new_context(viewport=VIEWPORT)
        token = secrets.token_urlsafe(32)
        now = time.time()
        state = LoginState(
            id="lgn_" + secrets.token_urlsafe(12),
            token_hash=_hash_token(token),
            owner=principal,
            domain=req.domain,
            label=req.label,
            refresh_session_id=req.session_id,
            created_at=now,
            expires_at=now + self.settings.login_timeout_seconds,
            context=context,
        )
        try:
            page = await context.new_page()
            state.page = page
            context.on("page", lambda new_page: self._on_new_page(state, new_page))
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
        except ApiError:
            await self._close_context(state)
            raise
        except Exception as exc:
            await self._close_context(state)
            raise ApiError(
                ErrorCode.FETCH_FAILED, f"Could not open the login page: {type(exc).__name__}"
            ) from exc
        self._logins[state.id] = state
        self.start_background()
        base = (self.settings.public_base_url or base_url).rstrip("/")
        viewer_url = f"{base}/sessions/login/{state.id}/view#token={token}"
        logger.info("Interactive login %s started for %s", state.id, req.domain)
        return state.info(viewer_url)

    def _on_new_page(self, state: LoginState, page: Any) -> None:
        # Popups (e.g. "Sign in with ...") become the active page until closed.
        previous = state.page
        state.page = page

        def _on_close(_: Any) -> None:
            if state.page is page:
                state.page = previous

        page.on("close", _on_close)

    def _get_owned(self, principal: Principal, login_id: str) -> LoginState:
        state = self._logins.get(login_id)
        if state is None or not secrets.compare_digest(state.owner.owner_id, principal.owner_id):
            raise ApiError(ErrorCode.LOGIN_NOT_FOUND, "Login not found.")
        return state

    def get(self, principal: Principal, login_id: str) -> LoginInfo:
        return self._get_owned(principal, login_id).info()

    def authorize_viewer(self, login_id: str, token: str) -> LoginState:
        state = self._logins.get(login_id)
        if state is None or not token:
            raise ApiError(ErrorCode.LOGIN_NOT_FOUND, "Login not found.")
        if not secrets.compare_digest(state.token_hash, _hash_token(token)):
            raise ApiError(ErrorCode.LOGIN_NOT_FOUND, "Login not found.")
        if state.status == "pending" and time.time() > state.expires_at:
            state.status = "expired"
        if state.status == "expired":
            raise ApiError(ErrorCode.LOGIN_EXPIRED, "This login has expired; start a new one.")
        return state

    async def complete_owned(self, principal: Principal, login_id: str) -> LoginInfo:
        return await self.complete(self._get_owned(principal, login_id))

    async def complete(self, state: LoginState) -> LoginInfo:
        async with state.lock:
            if state.status == "completed":
                return state.info()
            if state.status != "pending" or state.context is None:
                raise ApiError(ErrorCode.LOGIN_EXPIRED, f"Login is {state.status}.")
            raw = await state.context.storage_state()
            captured = StorageState.model_validate(raw).filter_domain(state.domain)
            profile = profile_for(state.domain)
            live = {c.name for c in captured.cookies if not c.is_expired()}
            if profile.auth_cookies and not any(name in live for name in profile.auth_cookies):
                raise ApiError(
                    ErrorCode.LOGIN_INCOMPLETE,
                    "You do not seem to be logged in yet (no login cookie found). "
                    "Finish logging in, then save again.",
                    details={"expected_cookies": list(profile.auth_cookies)},
                )
            if not captured.cookies:
                raise ApiError(
                    ErrorCode.LOGIN_INCOMPLETE, f"No cookies for {state.domain} were set yet."
                )
            if state.refresh_session_id:
                info = await self.sessions.replace_state(
                    state.owner, state.refresh_session_id, state=captured
                )
            else:
                info = await self.sessions.create(
                    state.owner, state.domain, captured, source="login", label=state.label
                )
            state.session = info
            state.status = "completed"
            state.finished_at = time.time()
            await self._close_context(state)
            logger.info("Interactive login %s completed -> session %s", state.id, info.id)
            return state.info()

    async def cancel(self, state: LoginState) -> LoginInfo:
        async with state.lock:
            if state.status == "pending":
                state.status = "cancelled"
                state.finished_at = time.time()
                await self._close_context(state)
            return state.info()

    async def cancel_owned(self, principal: Principal, login_id: str) -> LoginInfo:
        return await self.cancel(self._get_owned(principal, login_id))

    # ------------------------------------------------------------------ remote control
    async def screenshot(self, state: LoginState) -> bytes | None:
        page = state.page
        if page is None or state.status != "pending":
            return None
        try:
            return await page.screenshot(type="jpeg", quality=60)
        except Exception:
            return None

    async def page_status(self, state: LoginState) -> dict[str, Any]:
        page = state.page
        title = ""
        if page is not None:
            with contextlib.suppress(Exception):
                title = await page.title()
        return {
            "type": "status",
            "status": state.status,
            "url": getattr(page, "url", None),
            "title": title,
            "expires_at": int(state.expires_at),
        }

    async def handle_event(self, state: LoginState, event: dict[str, Any]) -> None:
        """Applies one input event from the viewer to the active page."""
        page = state.page
        if page is None or state.status != "pending":
            return
        kind = event.get("type")
        width, height = VIEWPORT

        def _xy() -> tuple[float, float]:
            x = float(event.get("x", 0))
            y = float(event.get("y", 0))
            return max(0.0, min(x, width - 1)), max(0.0, min(y, height - 1))

        actions: dict[str, Callable[[], Awaitable[Any]]] = {}
        if kind == "click":
            x, y = _xy()
            actions["click"] = lambda: page.mouse.click(x, y)
        elif kind == "dblclick":
            x, y = _xy()
            actions["dblclick"] = lambda: page.mouse.dblclick(x, y)
        elif kind == "wheel":
            dx = max(-2000.0, min(float(event.get("dx", 0)), 2000.0))
            dy = max(-2000.0, min(float(event.get("dy", 0)), 2000.0))
            actions["wheel"] = lambda: page.mouse.wheel(dx, dy)
        elif kind == "type":
            text = str(event.get("text", ""))[:500]
            actions["type"] = lambda: page.keyboard.type(text)
        elif kind == "key":
            key = str(event.get("key", ""))
            modifiers = [
                m for m in event.get("modifiers", []) if m in {"Shift", "Control", "Alt", "Meta"}
            ]
            # Printable characters arrive as "type" events; "key" is for special keys
            # and shortcuts such as Control+A.
            if key in ALLOWED_KEYS or (len(key) == 1 and modifiers):
                combo = "+".join([*modifiers, key])
                actions["key"] = lambda: page.keyboard.press(combo)
        elif kind == "back":
            actions["back"] = lambda: page.go_back(timeout=15_000)
        elif kind == "reload":
            actions["reload"] = lambda: page.reload(timeout=30_000)
        for action in actions.values():
            try:
                await action()
            except Exception as exc:  # navigation races are normal while logging in
                logger.debug("Login %s input ignored: %s", state.id, type(exc).__name__)
