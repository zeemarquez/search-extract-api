"""A3 — interactive login through the remote browser (fake browser, real protocol)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from search_extract_api.main import create_app
from search_extract_api.sessions.login import LoginManager

pytestmark = pytest.mark.a3

KEY_A = {"X-API-Key": "key-a"}
KEY_B = {"X-API-Key": "key-b"}
FUTURE = time.time() + 86400


class FakeMouse:
    def __init__(self, log: list) -> None:
        self.log = log

    async def click(self, x: float, y: float) -> None:
        self.log.append(("click", round(x), round(y)))

    async def dblclick(self, x: float, y: float) -> None:
        self.log.append(("dblclick", round(x), round(y)))

    async def wheel(self, dx: float, dy: float) -> None:
        self.log.append(("wheel", dx, dy))


class FakeKeyboard:
    def __init__(self, log: list) -> None:
        self.log = log

    async def type(self, text: str) -> None:
        self.log.append(("type", text))

    async def press(self, key: str) -> None:
        self.log.append(("press", key))


class FakePage:
    def __init__(self, log: list) -> None:
        self.log = log
        self.url = "about:blank"
        self.mouse = FakeMouse(log)
        self.keyboard = FakeKeyboard(log)
        self.frame = 0

    async def goto(self, url: str, **_: Any) -> None:
        self.url = url
        self.log.append(("goto", url))

    async def screenshot(self, **_: Any) -> bytes:
        self.frame += 1
        return b"\xff\xd8JPEG" + bytes([self.frame % 3])

    async def title(self) -> str:
        return "Log in"

    async def go_back(self, **_: Any) -> None:
        self.log.append(("back",))

    async def reload(self, **_: Any) -> None:
        self.log.append(("reload",))

    def on(self, *_: Any) -> None:
        pass


class FakeContext:
    def __init__(self) -> None:
        self.log: list = []
        self.state: dict = {"cookies": [], "origins": []}
        self.closed = False
        self.page: FakePage | None = None

    async def new_page(self) -> FakePage:
        self.page = FakePage(self.log)
        return self.page

    def on(self, *_: Any) -> None:
        pass

    async def storage_state(self) -> dict:
        return self.state

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    enabled = True
    status = {"enabled": True, "running": True, "error": None}

    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []

    async def new_context(self, **_: Any) -> FakeContext:
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        pass


LOGGED_IN_STATE = {
    "cookies": [
        {
            "name": "li_at",
            "value": "NEW-SECRET",
            "domain": ".linkedin.com",
            "path": "/",
            "expires": FUTURE,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "bcookie",
            "value": "b",
            "domain": ".linkedin.com",
            "path": "/",
            "expires": FUTURE,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "_ga",
            "value": "tracker",
            "domain": ".doubleclick.net",
            "path": "/",
            "expires": FUTURE,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        },
    ],
    "origins": [
        {"origin": "https://www.linkedin.com", "localStorage": [{"name": "k", "value": "v"}]}
    ],
}


@pytest.fixture
def app_and_browser(settings):
    app = create_app(settings.model_copy(update={"api_keys": "key-a,key-b"}))
    browser = FakeBrowser()
    with TestClient(app) as client:
        app.state.logins = LoginManager(app.state.settings, browser, app.state.sessions)  # type: ignore[arg-type]
        yield client, browser


def start_login(client: TestClient, **body: Any) -> dict:
    response = client.post(
        "/sessions/login", json={"domain": "linkedin.com", **body}, headers=KEY_A
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_start_returns_viewer_url_with_secret_in_fragment(app_and_browser) -> None:
    client, browser = app_and_browser
    login = start_login(client)
    assert login["status"] == "pending"
    assert login["login_id"].startswith("lgn_")
    assert "/sessions/login/" in login["viewer_url"] and "/view#token=" in login["viewer_url"]
    assert browser.contexts[0].log[0] == ("goto", "https://www.linkedin.com/login")
    # Only the starter can see it.
    assert (
        client.get(f"/sessions/login/{login['login_id']}", headers=KEY_A).json()["status"]
        == "pending"
    )
    other = client.get(f"/sessions/login/{login['login_id']}", headers=KEY_B)
    assert other.status_code == 404 and other.json()["error"]["code"] == "LOGIN_NOT_FOUND"


def test_login_url_must_belong_to_domain(app_and_browser) -> None:
    client, _ = app_and_browser
    response = client.post(
        "/sessions/login",
        json={"domain": "linkedin.com", "login_url": "https://evil.example.com/login"},
        headers=KEY_A,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_URL"


def test_complete_requires_login_cookie_then_stores_only_session_state(app_and_browser) -> None:
    client, browser = app_and_browser
    login = start_login(client, label="from login")
    login_id = login["login_id"]
    early = client.post(f"/sessions/login/{login_id}/complete", headers=KEY_A)
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "LOGIN_INCOMPLETE"

    browser.contexts[0].state = LOGGED_IN_STATE
    done = client.post(f"/sessions/login/{login_id}/complete", headers=KEY_A).json()
    assert done["status"] == "completed"
    session = done["session"]
    assert session["source"] == "login" and session["label"] == "from login"
    assert session["cookie_count"] == 2  # third-party cookie dropped
    assert session["local_storage_origins"] == 1
    assert browser.contexts[0].closed
    assert "NEW-SECRET" not in str(done)
    # The session belongs to the API key that started the login.
    assert [s["id"] for s in client.get("/sessions", headers=KEY_A).json()] == [session["id"]]
    assert client.get("/sessions", headers=KEY_B).json() == []


def test_viewer_page_and_websocket_protocol(app_and_browser) -> None:
    client, browser = app_and_browser
    login = start_login(client)
    login_id = login["login_id"]
    token = login["viewer_url"].split("#token=", 1)[1]

    page = client.get(f"/sessions/login/{login_id}/view")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "nonce-" in csp
    assert page.headers["cache-control"] == "no-store"
    assert token not in page.text

    with client.websocket_connect(f"/sessions/login/{login_id}/ws") as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        assert ws.receive_json()["type"] == "error"

    with client.websocket_connect(f"/sessions/login/{login_id}/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        first = ws.receive()
        assert first.get("bytes", b"").startswith(b"\xff\xd8")
        ws.send_json({"type": "click", "x": 640, "y": 400})
        ws.send_json({"type": "type", "text": "user@example.com"})
        ws.send_json({"type": "key", "key": "Enter", "modifiers": []})
        ws.send_json({"type": "key", "key": "a", "modifiers": ["Control"]})
        ws.send_json({"type": "key", "key": "q", "modifiers": []})  # printable keys must use "type"
        ws.send_json({"type": "wheel", "dx": 0, "dy": 300})
        browser.contexts[0].state = LOGGED_IN_STATE
        ws.send_json({"type": "done"})
        completed = None
        for _ in range(50):
            message = ws.receive()
            if message.get("text") and '"completed"' in message["text"]:
                completed = message["text"]
                break
        assert completed is not None and "ses_" in completed

    log = browser.contexts[0].log
    assert ("click", 640, 400) in log
    assert ("type", "user@example.com") in log
    assert ("press", "Enter") in log
    assert ("press", "Control+a") in log
    assert ("press", "q") not in log
    assert ("wheel", 0.0, 300.0) in log
    assert client.get(f"/sessions/login/{login_id}", headers=KEY_A).json()["status"] == "completed"


def test_cancel_and_expiry(app_and_browser) -> None:
    client, browser = app_and_browser
    login = start_login(client)
    cancelled = client.delete(f"/sessions/login/{login['login_id']}", headers=KEY_A).json()
    assert cancelled["status"] == "cancelled"
    assert browser.contexts[0].closed
    again = client.post(f"/sessions/login/{login['login_id']}/complete", headers=KEY_A)
    assert again.status_code == 410
    assert again.json()["error"]["code"] == "LOGIN_EXPIRED"


def test_browser_unavailable(settings) -> None:
    app = create_app(settings.model_copy(update={"api_keys": "key-a"}))
    with TestClient(app) as client:
        response = client.post("/sessions/login", json={"domain": "linkedin.com"}, headers=KEY_A)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BROWSER_UNAVAILABLE"
