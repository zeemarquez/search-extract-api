"""A4 — jobs vertical: LinkedIn (session required), Remotive, Arbeitnow."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from search_extract_api.verticals.jobs.schemas import JobResult
from search_extract_api.verticals.jobs.sources import guess_work_mode, parse_linkedin_cards

from .conftest import fixture_text

pytestmark = pytest.mark.a4

KEY_A = {"X-API-Key": "key-a"}
LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
FUTURE = time.time() + 30 * 86400
RECENT = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")

REMOTIVE = {
    "job-count": 2,
    "jobs": [
        {
            "id": 1,
            "url": "https://remotive.com/remote-jobs/software-dev/python-engineer-1",
            "title": "Python Engineer",
            "company_name": "Initech",
            "company_logo": "https://remotive.com/logo.png",
            "category": "Software Development",
            "tags": ["python", "django"],
            "job_type": "full_time",
            "publication_date": RECENT,
            "candidate_required_location": "Europe",
            "salary": "$80k - $100k",
            "description": "<p>Build APIs</p>",
        },
        {
            "id": 2,
            "url": "https://remotive.com/remote-jobs/software-dev/go-dev-2",
            "title": "Go Developer",
            "company_name": "Hooli",
            "tags": [],
            "job_type": "contract",
            "publication_date": RECENT,
            "candidate_required_location": "USA only",
            "salary": "",
            "description": "Go",
        },
    ],
}
ARBEITNOW = {
    "data": [
        {
            "slug": "python-dev-berlin",
            "company_name": "Umbrella",
            "title": "Senior Python Developer (m/w/d)",
            "description": "<p>Python and FastAPI</p>",
            "remote": False,
            "url": "https://www.arbeitnow.com/jobs/python-dev-berlin",
            "tags": ["Python"],
            "job_types": ["full time"],
            "location": "Berlin",
            "created_at": int(time.time()) - 3600,
        },
        {
            "slug": "java-remote",
            "company_name": "Stark",
            "title": "Java Developer",
            "description": "Java",
            "remote": True,
            "url": "https://www.arbeitnow.com/jobs/java-remote",
            "tags": ["Java"],
            "job_types": [],
            "location": "Munich",
            "created_at": int(time.time()) - 7200,
        },
    ],
    "links": {},
    "meta": {},
}
LINKEDIN_COOKIES = [
    {
        "domain": ".linkedin.com",
        "name": "li_at",
        "value": "LI-AT",
        "path": "/",
        "expirationDate": FUTURE,
    },
    {
        "domain": ".www.linkedin.com",
        "name": "JSESSIONID",
        "value": "ajax:1",
        "path": "/",
        "expirationDate": FUTURE,
    },
]


def mock_public_sources(net) -> None:
    net.get("https://remotive.com/api/remote-jobs").mock(
        return_value=httpx.Response(200, json=REMOTIVE)
    )
    net.get("https://www.arbeitnow.com/api/job-board-api").mock(
        return_value=httpx.Response(200, json=ARBEITNOW)
    )


def test_parse_linkedin_cards() -> None:
    jobs = parse_linkedin_cards(fixture_text("linkedin_jobs.html"))
    assert len(jobs) == 2
    first = jobs[0]
    assert first.url == "https://www.linkedin.com/jobs/view/4012345678/"
    assert first.job_title == "Desarrollador Python"
    assert first.company == "Acme"
    assert first.location == "Madrid, Comunidad de Madrid, España (Híbrido)"
    assert first.work_mode == "hybrid"
    assert first.salary == "40.000 € - 50.000 €/año"
    assert first.published_at.date().isoformat() == "2026-10-17"
    assert first.image == "https://media.licdn.com/dms/image/acme-logo.png"
    assert jobs[1].work_mode is None


def test_parse_linkedin_layout_change_is_an_error() -> None:
    from search_extract_api.errors import ApiError

    with pytest.raises(ApiError) as info:
        parse_linkedin_cards(
            "<html><body>" + "<div class='new-layout'>job</div>" * 200 + "</body></html>"
        )
    assert info.value.code == "PARSE_ERROR"
    assert parse_linkedin_cards("") == []


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("Madrid (Híbrido)", "hybrid"),
        ("Remote - Europe", "remote"),
        ("Presencial", "onsite"),
        ("Berlin", None),
    ],
)
def test_guess_work_mode(text, mode) -> None:
    assert guess_work_mode(text) == mode


async def test_jobs_without_session_skip_linkedin(client, net) -> None:
    mock_public_sources(net)
    body = (await client.get("/jobs/search", params={"q": "python"})).json()
    statuses = {s["name"]: s for s in body["sources"]}
    assert statuses["linkedin"]["skipped"] is True
    assert statuses["linkedin"]["details"]["reason"] == "requires session"
    assert body["partial"] is False
    urls = {r["url"] for r in body["results"]}
    assert "https://remotive.com/remote-jobs/software-dev/python-engineer-1" in urls
    assert "https://www.arbeitnow.com/jobs/python-dev-berlin" in urls
    assert "https://www.arbeitnow.com/jobs/java-remote" not in urls  # client-side keyword filter
    job = JobResult.model_validate(next(r for r in body["results"] if "remotive" in r["url"]))
    assert (
        job.work_mode == "remote"
        and job.company == "Initech"
        and job.employment_type == "full_time"
    )


async def test_explicit_linkedin_without_session_fails_clearly(client, net) -> None:
    body = (await client.get("/jobs/search", params={"q": "python", "sources": "linkedin"})).json()
    assert body["failed_sources"] == ["linkedin"]
    assert body["sources"][0]["error"]["code"] == "SESSION_REQUIRED"


async def test_jobs_filters(client, net) -> None:
    mock_public_sources(net)
    body = (
        await client.get(
            "/jobs/search", params={"q": "developer", "location": "munich", "work_mode": "remote"}
        )
    ).json()
    assert [r["url"] for r in body["results"]] == ["https://www.arbeitnow.com/jobs/java-remote"]
    response = await client.get("/jobs/search")
    assert response.status_code == 422


async def test_linkedin_with_session(make_client, net) -> None:
    mock_public_sources(net)
    check = net.get("https://www.linkedin.com/feed/").mock(
        return_value=httpx.Response(
            200,
            text="<html><body>" + "<p>feed</p>" * 200 + "</body></html>",
            headers={"content-type": "text/html"},
        )
    )
    search = net.get(LINKEDIN_SEARCH).mock(
        return_value=httpx.Response(
            200, text=fixture_text("linkedin_jobs.html"), headers={"content-type": "text/html"}
        )
    )
    async with make_client(api_keys="key-a") as c:
        sid = (
            await c.post(
                "/sessions",
                json={"domain": "linkedin.com", "cookies": LINKEDIN_COOKIES},
                headers=KEY_A,
            )
        ).json()["id"]
        params = {
            "q": "python",
            "location": "Madrid",
            "work_mode": "hybrid",
            "time_range": "week",
            "session_id": sid,
        }
        body = (await c.get("/jobs/search", params=params, headers=KEY_A)).json()
        assert body["failed_sources"] == []
        linkedin = [r for r in body["results"] if "linkedin" in r["providers"]]
        assert {r["job_title"] for r in linkedin} == {"Desarrollador Python", "Backend Engineer"}
        # The filter is forwarded, but modes come from each card, never from the query.
        modes = {r["job_title"]: r["work_mode"] for r in linkedin}
        assert modes == {"Desarrollador Python": "hybrid", "Backend Engineer": None}
        sent = search.calls.last.request
        assert sent.url.params["keywords"] == "python"
        assert sent.url.params["location"] == "Madrid"
        assert sent.url.params["f_WT"] == "3"
        assert sent.url.params["f_TPR"].startswith("r")
        assert "li_at=LI-AT" in sent.headers["cookie"]
        # The login itself is verified once and then remembered for a while.
        await c.get("/jobs/search", params={**params, "no_cache": "true"}, headers=KEY_A)
        assert check.call_count == 1


async def test_linkedin_expired_session_is_reported_not_empty(make_client, net) -> None:
    mock_public_sources(net)
    net.get("https://www.linkedin.com/feed/").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/authwall?trk=feed"}
        )
    )
    net.get("https://www.linkedin.com/authwall?trk=feed").mock(
        return_value=httpx.Response(
            200, text="<html><body>Sign in</body></html>", headers={"content-type": "text/html"}
        )
    )
    search = net.get(LINKEDIN_SEARCH).mock(
        return_value=httpx.Response(200, text=fixture_text("linkedin_jobs.html"))
    )
    async with make_client(api_keys="key-a") as c:
        sid = (
            await c.post(
                "/sessions",
                json={"domain": "linkedin.com", "cookies": LINKEDIN_COOKIES},
                headers=KEY_A,
            )
        ).json()["id"]
        body = (
            await c.get("/jobs/search", params={"q": "python", "session_id": sid}, headers=KEY_A)
        ).json()
        assert body["failed_sources"] == ["linkedin"]
        status = next(s for s in body["sources"] if s["name"] == "linkedin")
        assert status["error"]["code"] == "SESSION_EXPIRED"
        assert not search.called  # no silent guest-mode fallback
        assert body["count"] > 0  # other sources still answer
        session = (await c.get(f"/sessions/{sid}", headers=KEY_A)).json()
        assert session["status"] == "expired"


async def test_linkedin_http_999_is_reported_as_blocked(make_client, net) -> None:
    net.get("https://www.linkedin.com/feed/").mock(
        return_value=httpx.Response(
            200, text="<html><body>" + "<p>feed</p>" * 200 + "</body></html>"
        )
    )
    net.get(LINKEDIN_SEARCH).mock(
        return_value=httpx.Response(999, text="<html><body></body></html>")
    )
    async with make_client(api_keys="key-a") as c:
        sid = (
            await c.post(
                "/sessions",
                json={"domain": "linkedin.com", "cookies": LINKEDIN_COOKIES},
                headers=KEY_A,
            )
        ).json()["id"]
        body = (
            await c.get(
                "/jobs/search",
                params={"q": "python", "session_id": sid, "sources": "linkedin"},
                headers=KEY_A,
            )
        ).json()
    assert body["sources"][0]["error"]["code"] == "SOURCE_BLOCKED"


async def test_expired_session_only_fails_the_sources_that_need_it(make_client, net) -> None:
    mock_public_sources(net)
    expired = [dict(LINKEDIN_COOKIES[0], expirationDate=time.time() - 60), LINKEDIN_COOKIES[1]]
    async with make_client(api_keys="key-a") as c:
        sid = (
            await c.post(
                "/sessions", json={"domain": "linkedin.com", "cookies": expired}, headers=KEY_A
            )
        ).json()["id"]
        response = await c.get(
            "/jobs/search", params={"q": "python", "session_id": sid}, headers=KEY_A
        )
    assert response.status_code == 200
    body = response.json()
    assert body["failed_sources"] == ["linkedin"]
    linkedin = next(s for s in body["sources"] if s["name"] == "linkedin")
    assert linkedin["error"]["code"] == "SESSION_EXPIRED"
    assert linkedin["details"]["session_id"] == sid
    assert body["count"] > 0  # Remotive / Arbeitnow still answer


async def test_remotive_is_fetched_once_and_filtered_locally(client, net) -> None:
    route = net.get("https://remotive.com/api/remote-jobs").mock(
        return_value=httpx.Response(200, json=REMOTIVE)
    )
    net.get("https://www.arbeitnow.com/api/job-board-api").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    python = (
        await client.get("/jobs/search", params={"q": "django", "sources": "remotive"})
    ).json()
    go = (
        await client.get("/jobs/search", params={"q": "go developer", "sources": "remotive"})
    ).json()
    assert [r["job_title"] for r in python["results"]] == ["Python Engineer"]  # matched on tags
    assert [r["job_title"] for r in go["results"]] == ["Go Developer"]
    assert route.call_count == 1  # one upstream request serves every query
    assert "search" not in route.calls.last.request.url.params
    assert python["results"][0]["extra"]["attribution"] == "Remotive"
