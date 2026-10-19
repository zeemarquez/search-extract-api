"""Job sources: LinkedIn (with session), Remotive, Arbeitnow and a web-search source."""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any
from urllib.parse import urlencode

from lxml import html as lxml_html

from ...adapters.base import SourceAdapter, SourceContext
from ...adapters.searxng import SearxngAdapter
from ...config import Settings
from ...core.dates import parse_datetime, resolve_window, utcnow
from ...core.text import collapse_ws, split_title_outlet, strip_accents, strip_html, truncate
from ...core.urls import is_http_url, site_of
from ...errors import ApiError, ErrorCode
from ...schemas.common import BaseQuery, Result
from ...sessions.profiles import DomainProfile
from .schemas import JobResult, WorkMode

LINKEDIN_PROFILE = DomainProfile(
    domain="linkedin.com",
    check_url="https://www.linkedin.com/feed/",
    login_url="https://www.linkedin.com/login",
    auth_cookies=("li_at",),
    login_path_patterns=(
        r"^/authwall",
        r"^/login",
        r"^/uas/login",
        r"^/checkpoint/lg/",
        r"^/signup",
    ),
    captcha_markers=(r"/checkpoint/challenge",),
    notes=(
        "Import cookies from a logged-in browser (li_at and JSESSIONID are the important ones) "
        "or use the interactive login.",
    ),
)

LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_PAGE_SIZE = 10
LINKEDIN_WORK_MODE = {"onsite": "1", "remote": "2", "hybrid": "3"}
_LINKEDIN_JOB_ID = re.compile(r"(?:jobPosting:|/jobs/view/(?:[^/?#]*-)?)(\d{6,})")
_MODE_HINTS: tuple[tuple[str, WorkMode], ...] = (
    ("hybrid", "hybrid"),
    ("híbrido", "hybrid"),
    ("hibrido", "hybrid"),
    ("remote", "remote"),
    ("remoto", "remote"),
    ("teletrabajo", "remote"),
    ("on-site", "onsite"),
    ("onsite", "onsite"),
    ("presencial", "onsite"),
)


def guess_work_mode(*texts: str | None) -> WorkMode | None:
    joined = " ".join(t for t in texts if t).lower()
    for hint, mode in _MODE_HINTS:
        if hint in joined:
            return mode
    return None


def _text(element: Any, xpath: str) -> str | None:
    found = element.xpath(xpath)
    if not found:
        return None
    node = found[0]
    value = node if isinstance(node, str) else node.text_content()
    return collapse_ws(value) or None


def parse_linkedin_cards(html_text: str, work_mode: WorkMode | None = None) -> list[JobResult]:
    """Parses job cards from LinkedIn's jobs search HTML."""
    if not html_text or not html_text.strip():
        return []
    tree = lxml_html.fromstring(html_text)
    cards = tree.xpath("//*[@data-entity-urn][contains(@data-entity-urn, 'jobPosting')]")
    if not cards:
        cards = tree.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' base-search-card ')]"
        )
    results: list[JobResult] = []
    for card in cards:
        urn = card.get("data-entity-urn") or ""
        href = _text(card, ".//a[contains(@class, 'base-card__full-link')]/@href") or _text(
            card, ".//a/@href"
        )
        match = _LINKEDIN_JOB_ID.search(urn) or _LINKEDIN_JOB_ID.search(href or "")
        if not match:
            continue
        job_id = match.group(1)
        title = _text(card, ".//*[contains(@class, 'base-search-card__title')]") or _text(
            card, ".//span[contains(@class, 'sr-only')]"
        )
        company = _text(card, ".//*[contains(@class, 'base-search-card__subtitle')]")
        location = _text(card, ".//*[contains(@class, 'job-search-card__location')]")
        salary = _text(card, ".//*[contains(@class, 'job-search-card__salary-info')]")
        posted = _text(card, ".//time/@datetime")
        image = _text(card, ".//img/@data-delayed-url") or _text(card, ".//img/@src")
        results.append(
            JobResult(
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                title=title,
                job_title=title,
                company=company,
                location=location,
                work_mode=work_mode or guess_work_mode(location, title),
                salary=salary,
                published_at=parse_datetime(posted),
                image=image if is_http_url(image) else None,
                source="linkedin.com",
                providers=["linkedin"],
                extra={"job_id": job_id},
            )
        )
    if not results and len(html_text) > 3000:
        raise ApiError(
            ErrorCode.PARSE_ERROR,
            "LinkedIn returned a page without recognizable job cards (layout may have changed).",
        )
    return results


class LinkedInJobsAdapter(SourceAdapter):
    name = "linkedin"
    title = "LinkedIn Jobs"
    description = "LinkedIn job search using your own LinkedIn session."
    result_model = JobResult
    session_policy = "required"
    session_domain = "linkedin.com"

    def notes(self, settings: Settings) -> list[str]:
        return [
            "Requires a LinkedIn session (session_id). Automated access may break LinkedIn's "
            "terms of service and can get the account restricted: use at your own risk.",
        ]

    async def _ensure_session_valid(self, ctx: SourceContext) -> None:
        """Logged-out sessions can still read public pages, so verify the login itself
        (at most every 30 minutes) to report expiry instead of silently degrading."""
        session = ctx.session
        assert session is not None
        key = f"sesok:{session.id}"
        if await ctx.cache.get(key):
            return
        await ctx.fetch_page(LINKEDIN_PROFILE.check_url, session=session, mode="light")
        await ctx.cache.set(key, b"1", 1800)

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        if ctx.session is None:
            raise ApiError(ErrorCode.SESSION_REQUIRED, "LinkedIn requires a session.")
        await self._ensure_session_valid(ctx)
        params: dict[str, str] = {}
        keywords = " ".join(filter(None, [query.q, getattr(query, "company", None)]))
        if keywords:
            params["keywords"] = keywords
        if getattr(query, "location", None):
            params["location"] = query.location  # type: ignore[attr-defined]
        work_mode = getattr(query, "work_mode", None)
        if work_mode:
            params["f_WT"] = LINKEDIN_WORK_MODE[work_mode]
        since, _ = resolve_window(query.time_range, query.since, query.until)
        if since is not None:
            params["f_TPR"] = f"r{max(3600, int((utcnow() - since).total_seconds()))}"
        if query.sort == "date":
            params["sortBy"] = "DD"
        pages = min(3, max(1, math.ceil(query.limit / LINKEDIN_PAGE_SIZE)))
        results: list[Result] = []
        for page in range(pages):
            start = ((query.page - 1) * pages + page) * LINKEDIN_PAGE_SIZE
            url = f"{LINKEDIN_SEARCH}?{urlencode({**params, 'start': str(start)})}"
            try:
                fetched = await ctx.fetch_page(url, session=ctx.session, mode="light")
            except ApiError as exc:
                if exc.details.get("status_code") == 999:
                    raise ApiError(
                        ErrorCode.SOURCE_BLOCKED, "LinkedIn blocked the request (HTTP 999)."
                    ) from exc
                if results and exc.code == ErrorCode.UPSTREAM_HTTP_ERROR:
                    break
                raise
            batch = parse_linkedin_cards(fetched.html, work_mode)
            results.extend(batch)
            if len(batch) < LINKEDIN_PAGE_SIZE:
                break
        return results


def _matches_terms(haystack: str, terms: list[str]) -> bool:
    folded = strip_accents(haystack).casefold()
    return all(term in folded for term in terms)


def _terms(text: str | None) -> list[str]:
    return [strip_accents(t).casefold() for t in (text or "").split() if t.strip()]


class RemotiveAdapter(SourceAdapter):
    name = "remotive"
    title = "Remotive"
    description = "Remote jobs from remotive.com (public API, attribution required)."
    result_model = JobResult

    def notes(self, settings: Settings) -> list[str]:
        return ["Public API: keep request volume low and link back to Remotive job pages."]

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        work_mode = getattr(query, "work_mode", None)
        if work_mode and work_mode != "remote":
            return []
        params: dict[str, Any] = {"limit": min(200, max(query.limit * 3, 50))}
        keywords = " ".join(filter(None, [query.q, getattr(query, "company", None)]))
        if keywords:
            params["search"] = keywords
        data = await ctx.http.get_json(
            "https://remotive.com/api/remote-jobs", params=params, source="Remotive"
        )
        location_terms = _terms(getattr(query, "location", None))
        results: list[Result] = []
        for job in (data or {}).get("jobs") or []:
            url = job.get("url")
            if not is_http_url(url):
                continue
            region = job.get("candidate_required_location") or ""
            if location_terms and not (
                _matches_terms(region, location_terms)
                or re.search(r"worldwide|anywhere|global", region, re.I)
            ):
                continue
            company = job.get("company_name")
            if getattr(query, "company", None) and not _matches_terms(
                company or "",
                _terms(query.company),  # type: ignore[attr-defined]
            ):
                continue
            title = strip_html(job.get("title")) or None
            results.append(
                JobResult(
                    url=url,
                    title=title,
                    job_title=title,
                    company=company,
                    location=region or None,
                    work_mode="remote",
                    employment_type=job.get("job_type") or None,
                    salary=job.get("salary") or None,
                    published_at=parse_datetime(job.get("publication_date")),
                    snippet=truncate(strip_html(job.get("description")), 400) or None,
                    image=job.get("company_logo") if is_http_url(job.get("company_logo")) else None,
                    source="remotive.com",
                    providers=[self.name],
                    extra={"category": job.get("category"), "tags": job.get("tags") or []},
                )
            )
        return results


class ArbeitnowAdapter(SourceAdapter):
    name = "arbeitnow"
    title = "Arbeitnow"
    description = "Jobs in Europe (mostly Germany) from the Arbeitnow public job board API."
    result_model = JobResult
    pages = 3

    async def _page(self, ctx: SourceContext, page: int) -> list[dict[str, Any]]:
        data = await ctx.http.get_json(
            "https://www.arbeitnow.com/api/job-board-api", params={"page": page}, source="Arbeitnow"
        )
        return list((data or {}).get("data") or [])

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        pages = await asyncio.gather(
            *(self._page(ctx, page) for page in range(1, self.pages + 1)), return_exceptions=True
        )
        if isinstance(pages[0], BaseException):
            raise pages[0]
        terms = _terms(query.q)
        company_terms = _terms(getattr(query, "company", None))
        location_terms = _terms(getattr(query, "location", None))
        work_mode = getattr(query, "work_mode", None)
        results: list[Result] = []
        for page in pages:
            if isinstance(page, BaseException):
                continue
            for job in page:
                url = job.get("url")
                if not is_http_url(url):
                    continue
                title = strip_html(job.get("title")) or None
                tags = [str(t) for t in job.get("tags") or []]
                body = " ".join([title or "", " ".join(tags), strip_html(job.get("description"))])
                if terms and not _matches_terms(body, terms):
                    continue
                if company_terms and not _matches_terms(
                    job.get("company_name") or "", company_terms
                ):
                    continue
                location = job.get("location") or ""
                remote = bool(job.get("remote"))
                if location_terms and not _matches_terms(location, location_terms):
                    continue
                mode: WorkMode | None = "remote" if remote else guess_work_mode(location, title)
                if work_mode and mode != work_mode:
                    continue
                job_types = job.get("job_types") or []
                results.append(
                    JobResult(
                        url=url,
                        title=title,
                        job_title=title,
                        company=job.get("company_name") or None,
                        location=location or None,
                        work_mode=mode,
                        employment_type=", ".join(job_types) if job_types else None,
                        published_at=parse_datetime(job.get("created_at")),
                        snippet=truncate(strip_html(job.get("description")), 400) or None,
                        source="arbeitnow.com",
                        providers=[self.name],
                        extra={"tags": tags},
                    )
                )
        return results


JOB_SITES = (
    "infojobs.net",
    "indeed.com",
    "glassdoor.com",
    "tecnoempleo.com",
    "welcometothejungle.com",
    "linkedin.com/jobs/view",
)


class WebJobsAdapter(SearxngAdapter):
    """Job postings found through general web search on well-known job boards."""

    default_enabled = False

    def __init__(self) -> None:
        super().__init__(
            name="web_jobs",
            category="general",
            result_model=JobResult,
            title="Web search (job boards)",
            description="SearXNG restricted to well-known job boards (opt-in, heuristic parsing).",
        )

    def build_query(self, query: BaseQuery) -> str:
        parts = [
            query.q or "",
            getattr(query, "company", None) or "",
            getattr(query, "location", None) or "",
        ]
        sites = " OR ".join(f"site:{site}" for site in JOB_SITES)
        return f"{' '.join(p for p in parts if p)} ({sites})".strip()

    def to_result(self, item: dict[str, Any]) -> Result | None:
        base = super().to_result(item)
        if base is None:
            return None
        title = base.title or ""
        head, company = split_title_outlet(title)
        site = site_of(base.url)
        if company and site.split(".")[0] in company.lower():
            head, company = split_title_outlet(head)
        return JobResult(
            **{k: v for k, v in base.model_dump().items() if k in Result.model_fields},
            job_title=head or None,
            company=company,
            work_mode=guess_work_mode(title, base.snippet),
        )
