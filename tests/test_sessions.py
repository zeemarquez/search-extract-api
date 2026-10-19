"""A3 — authenticated sessions: import, ownership, encryption, expiry and health."""

from __future__ import annotations

import logging
import sqlite3
import time

import httpx
import pytest

pytestmark = pytest.mark.a3

KEY_A = {"X-API-Key": "key-a"}
KEY_B = {"X-API-Key": "key-b"}
FUTURE = time.time() + 30 * 86400
PAST = time.time() - 86400

LINKEDIN_COOKIES = [  # Cookie-Editor export format
    {
        "domain": ".linkedin.com",
        "name": "li_at",
        "value": "SECRET-LI-AT-VALUE",
        "path": "/",
        "expirationDate": FUTURE,
        "hostOnly": False,
        "httpOnly": True,
        "secure": True,
        "sameSite": "no_restriction",
    },
    {
        "domain": ".www.linkedin.com",
        "name": "JSESSIONID",
        "value": '"ajax:123"',
        "path": "/",
        "expirationDate": FUTURE,
        "hostOnly": False,
        "secure": True,
        "sameSite": "no_restriction",
    },
    {
        "domain": ".google.com",
        "name": "NID",
        "value": "OTHER-SITE",
        "path": "/",
        "expirationDate": FUTURE,
    },
]

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    f".intranet.example.com\tTRUE\t/\tTRUE\t{int(FUTURE)}\tsid\tSECRET-SID\n"
    f"#HttpOnly_intranet.example.com\tFALSE\t/reports\tFALSE\t{int(FUTURE)}\tpref\tdark\n"
    f".other.com\tTRUE\t/\tFALSE\t{int(FUTURE)}\ttrack\tnope\n"
)


@pytest.fixture
async def keyed(make_client):
    async with make_client(api_keys="alice:key-a,bob:key-b") as c:
        yield c


async def import_linkedin(c, headers=KEY_A, cookies=None) -> dict:
    response = await c.post(
        "/sessions",
        json={
            "domain": "https://www.linkedin.com/",
            "cookies": cookies or LINKEDIN_COOKIES,
            "label": "mine",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_auth_required_when_keys_configured(keyed) -> None:
    response = await keyed.get("/sessions")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert (await keyed.get("/sessions", headers={"X-API-Key": "wrong"})).status_code == 401
    assert (
        await keyed.get("/sessions", headers={"Authorization": "Bearer key-a"})
    ).status_code == 200
    assert (await keyed.get("/health")).status_code == 200  # health stays public


async def test_import_json_cookies_never_returns_values(keyed) -> None:
    info = await import_linkedin(keyed)
    assert info["domain"] == "linkedin.com"
    assert info["cookie_count"] == 2  # the google.com cookie was dropped
    assert info["source"] == "import"
    assert info["status"] == "unknown"
    assert info["label"] == "mine"
    assert info["expires_at"] is not None
    assert info["id"].startswith("ses_")
    listed = (await keyed.get("/sessions", headers=KEY_A)).json()
    assert [s["id"] for s in listed] == [info["id"]]
    for payload in (
        info,
        listed,
        (await keyed.get(f"/sessions/{info['id']}", headers=KEY_A)).json(),
    ):
        assert "SECRET-LI-AT-VALUE" not in str(payload)


async def test_import_netscape_and_storage_state(keyed) -> None:
    response = await keyed.post(
        "/sessions", json={"domain": "intranet.example.com", "cookies": NETSCAPE}, headers=KEY_A
    )
    assert response.status_code == 201
    assert response.json()["cookie_count"] == 2
    state = {
        "cookies": [
            {
                "name": "sid",
                "value": "v",
                "domain": "app.example.org",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [
            {"origin": "https://app.example.org", "localStorage": [{"name": "token", "value": "t"}]}
        ],
    }
    response = await keyed.post(
        "/sessions", json={"domain": "example.org", "cookies": state}, headers=KEY_A
    )
    assert response.status_code == 201
    assert response.json()["local_storage_origins"] == 1


@pytest.mark.parametrize(
    "cookies",
    [
        "not cookies at all",
        [{"name": "x"}],
        {"nope": []},
        [{"domain": ".other.com", "name": "a", "value": "b"}],
    ],
)
async def test_invalid_cookie_imports(keyed, cookies) -> None:
    response = await keyed.post(
        "/sessions", json={"domain": "linkedin.com", "cookies": cookies}, headers=KEY_A
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SESSION_INVALID"


async def test_session_only_usable_by_its_owner(keyed, net) -> None:
    info = await import_linkedin(keyed)
    sid = info["id"]
    # Another API key cannot see, use, check or delete it (and cannot tell it exists).
    for method, path in (
        ("GET", f"/sessions/{sid}"),
        ("POST", f"/sessions/{sid}/check"),
        ("DELETE", f"/sessions/{sid}"),
        ("PATCH", f"/sessions/{sid}"),
    ):
        response = await keyed.request(
            method, path, headers=KEY_B, json={"label": "x"} if method == "PATCH" else None
        )
        assert response.status_code == 404, (method, path)
        assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert (await keyed.get("/sessions", headers=KEY_B)).json() == []
    response = await keyed.post(
        "/extract",
        json={"url": "https://www.linkedin.com/jobs/view/1/", "session_id": sid},
        headers=KEY_B,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"
    response = await keyed.get(
        "/jobs/search",
        params={"q": "python", "session_id": sid, "sources": "linkedin"},
        headers=KEY_B,
    )
    assert response.status_code == 404
    # The owner can.
    assert (await keyed.get(f"/sessions/{sid}", headers=KEY_A)).status_code == 200


async def test_sessions_are_encrypted_at_rest(keyed, settings) -> None:
    await import_linkedin(keyed)
    raw = (
        sqlite3.connect(settings.sessions_db_path)
        .execute("SELECT owner_id, data FROM sessions")
        .fetchone()
    )
    owner_id, data = raw
    assert b"SECRET-LI-AT-VALUE" not in data
    assert data.startswith(b"gAAAAA")  # Fernet token
    assert "key-a" not in owner_id  # only a hash of the API key is stored


async def test_expired_cookies_give_session_expired_not_empty(keyed, net) -> None:
    expired = [dict(LINKEDIN_COOKIES[0], expirationDate=PAST), LINKEDIN_COOKIES[1]]
    info = await import_linkedin(keyed, cookies=expired)
    response = await keyed.post(
        "/extract",
        json={"url": "https://www.linkedin.com/jobs/view/1/", "session_id": info["id"]},
        headers=KEY_A,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    status = (await keyed.get(f"/sessions/{info['id']}", headers=KEY_A)).json()
    assert status["status"] == "expired"


async def test_login_wall_with_session_is_session_expired(keyed, net) -> None:
    info = await import_linkedin(keyed)
    net.get("https://www.linkedin.com/jobs/view/1/").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/authwall?trk=x"}
        )
    )
    net.get("https://www.linkedin.com/authwall?trk=x").mock(
        return_value=httpx.Response(
            200,
            text="<html><body>Join LinkedIn</body></html>",
            headers={"content-type": "text/html"},
        )
    )
    response = await keyed.post(
        "/extract",
        json={"url": "https://www.linkedin.com/jobs/view/1/", "session_id": info["id"]},
        headers=KEY_A,
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SESSION_EXPIRED"
    assert error["details"]["session_id"] == info["id"]
    status = (await keyed.get(f"/sessions/{info['id']}", headers=KEY_A)).json()
    assert status["status"] == "expired" and "login" in status["status_reason"]


async def test_captcha_with_session(keyed, net) -> None:
    info = await import_linkedin(keyed)
    net.get("https://www.linkedin.com/jobs/view/2/").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/checkpoint/challenge/abc"}
        )
    )
    net.get("https://www.linkedin.com/checkpoint/challenge/abc").mock(
        return_value=httpx.Response(
            200, text="<html><body>Let's do a quick security check</body></html>"
        )
    )
    response = await keyed.post(
        "/extract",
        json={"url": "https://www.linkedin.com/jobs/view/2/", "session_id": info["id"]},
        headers=KEY_A,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CAPTCHA_REQUIRED"
    status = (await keyed.get(f"/sessions/{info['id']}", headers=KEY_A)).json()
    assert status["status"] == "captcha"


async def test_cookies_only_sent_where_they_belong(keyed, net) -> None:
    response = await keyed.post(
        "/sessions", json={"domain": "intranet.example.com", "cookies": NETSCAPE}, headers=KEY_A
    )
    sid = response.json()["id"]
    page = (
        "<html><body>"
        + "<p>Quarterly report with plenty of real content inside.</p>" * 30
        + "</body></html>"
    )
    reports = net.get("https://intranet.example.com/reports/q3").mock(
        return_value=httpx.Response(200, text=page, headers={"content-type": "text/html"})
    )
    body = (
        await keyed.post(
            "/extract",
            json={"url": "https://intranet.example.com/reports/q3", "session_id": sid},
            headers=KEY_A,
        )
    ).json()
    assert body["session_id"] == sid
    sent = reports.calls.last.request.headers["cookie"]
    assert "pref=dark" in sent and "sid=SECRET-SID" in sent and "track" not in sent
    assert "SECRET-SID" not in str(body)
    info = (await keyed.get(f"/sessions/{sid}", headers=KEY_A)).json()
    assert info["status"] == "active" and info["last_used_at"] is not None
    # Results obtained with a session are never cached.
    await keyed.post(
        "/extract",
        json={"url": "https://intranet.example.com/reports/q3", "session_id": sid},
        headers=KEY_A,
    )
    assert reports.call_count == 2


async def test_session_domain_mismatch(keyed) -> None:
    info = await import_linkedin(keyed)
    response = await keyed.post(
        "/extract", json={"url": "https://example.com/", "session_id": info["id"]}, headers=KEY_A
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SESSION_DOMAIN_MISMATCH"


async def test_session_health_check(keyed, net) -> None:
    info = await import_linkedin(keyed)
    feed = net.get("https://www.linkedin.com/feed/").mock(
        return_value=httpx.Response(
            200,
            text="<html><body>" + "<p>Feed post</p>" * 100 + "</body></html>",
            headers={"content-type": "text/html"},
        )
    )
    check = (await keyed.post(f"/sessions/{info['id']}/check", headers=KEY_A)).json()
    assert check["status"] == "active"
    assert check["checked_url"] == "https://www.linkedin.com/feed/"
    assert check["session"]["last_checked_at"] is not None
    assert "li_at=SECRET-LI-AT-VALUE" in feed.calls.last.request.headers["cookie"]

    net.get("https://www.linkedin.com/feed/").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/login?session_redirect=x"}
        )
    )
    net.get("https://www.linkedin.com/login?session_redirect=x").mock(
        return_value=httpx.Response(200, text="<html><body><input type='password'></body></html>")
    )
    check = (await keyed.post(f"/sessions/{info['id']}/check", headers=KEY_A)).json()
    assert check["status"] == "expired"
    assert check["session"]["status"] == "expired"


async def test_session_request_budget(make_client, net) -> None:
    async with make_client(api_keys="key-a", session_max_requests_per_hour=2) as c:
        response = await c.post(
            "/sessions", json={"domain": "intranet.example.com", "cookies": NETSCAPE}, headers=KEY_A
        )
        sid = response.json()["id"]
        page = "<html><body>" + "<p>Some content for the report page.</p>" * 30 + "</body></html>"
        net.get("https://intranet.example.com/a").mock(
            return_value=httpx.Response(200, text=page, headers={"content-type": "text/html"})
        )
        payload = {"url": "https://intranet.example.com/a", "session_id": sid}
        assert (await c.post("/extract", json=payload, headers=KEY_A)).status_code == 200
        assert (await c.post("/extract", json=payload, headers=KEY_A)).status_code == 200
        third = await c.post("/extract", json=payload, headers=KEY_A)
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "SESSION_RATE_LIMITED"


async def test_update_and_delete(keyed) -> None:
    info = await import_linkedin(keyed)
    sid = info["id"]
    updated = (await keyed.patch(f"/sessions/{sid}", json={"label": "work"}, headers=KEY_A)).json()
    assert updated["label"] == "work"
    replaced = (
        await keyed.patch(f"/sessions/{sid}", json={"cookies": LINKEDIN_COOKIES[:1]}, headers=KEY_A)
    ).json()
    assert replaced["cookie_count"] == 1 and replaced["status"] == "unknown"
    assert (await keyed.delete(f"/sessions/{sid}", headers=KEY_A)).status_code == 204
    assert (await keyed.get(f"/sessions/{sid}", headers=KEY_A)).status_code == 404


async def test_secrets_never_logged(keyed, net, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    info = await import_linkedin(keyed)
    net.get("https://www.linkedin.com/feed/").mock(return_value=httpx.Response(500))
    await keyed.post(f"/sessions/{info['id']}/check", headers=KEY_A)
    assert "SECRET-LI-AT-VALUE" not in caplog.text
    assert "key-a" not in caplog.text


async def test_supported_sites(client) -> None:
    sites = {s["domain"]: s for s in (await client.get("/sessions/sites")).json()}
    assert sites["linkedin.com"]["auth_cookies"] == ["li_at"]
    assert "idealista.com" in sites
