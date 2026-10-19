"""Multi-source search orchestration shared by ``/search`` and every vertical.

Runs the selected adapters concurrently, applies the fallback policy, isolates
failures (a failing source never fails the request), merges and deduplicates the
results and caches the response.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..adapters.base import SourceAdapter, SourceContext
from ..config import Settings
from ..core.cache import Cache, make_key
from ..core.dates import resolve_window
from ..core.http import HttpService
from ..core.lang import detect_language
from ..core.merge import merge_results
from ..core.security import Principal
from ..core.urls import domain_matches
from ..errors import ApiError, ErrorCode
from ..schemas.common import BaseQuery, ErrorInfo, Result, SearchResponse, SourceStatus
from ..verticals.base import VerticalRegistry

if TYPE_CHECKING:
    from ..extraction.service import ExtractionService
    from ..sessions.models import SessionHandle
    from ..sessions.service import SessionService

logger = logging.getLogger(__name__)

_SESSION_STATE_ERRORS = {ErrorCode.SESSION_EXPIRED, ErrorCode.CAPTCHA_REQUIRED}


@dataclass
class _Run:
    adapter: SourceAdapter
    status: SourceStatus
    results: list[Result] = field(default_factory=list)


def _fail(
    name: str, code: ErrorCode | str, message: str, *, fallback: bool = False
) -> SourceStatus:
    return SourceStatus(
        name=name, ok=False, fallback=fallback, error=ErrorInfo(code=str(code), message=message)
    )


class SearchService:
    def __init__(
        self,
        *,
        settings: Settings,
        http: HttpService,
        cache: Cache,
        registry: VerticalRegistry,
        sessions: SessionService | None,
        extraction: ExtractionService | None,
    ) -> None:
        self.settings = settings
        self.http = http
        self.cache = cache
        self.registry = registry
        self.sessions = sessions
        self.extraction = extraction

    # ------------------------------------------------------------------ helpers
    def context(self, session: SessionHandle | None) -> SourceContext:
        fetch_page = self.extraction.fetch_page if self.extraction else None
        return SourceContext(
            settings=self.settings,
            http=self.http,
            cache=self.cache,
            fetch_page=fetch_page,
            session=session,
        )

    def _preflight(
        self,
        adapter: SourceAdapter,
        explicit: bool,
        session: SessionHandle | None,
        session_error: ApiError | None = None,
    ) -> tuple[SourceStatus | None, SessionHandle | None]:
        """Returns (status_if_not_runnable, session_to_pass)."""
        name = adapter.name
        if not adapter.configured(self.settings):
            if explicit or not adapter.optional_config:
                return (
                    _fail(
                        name,
                        ErrorCode.SOURCE_NOT_CONFIGURED,
                        f"Source '{name}' is not configured.",
                        fallback=adapter.fallback,
                    ),
                    None,
                )
            return SourceStatus(
                name=name,
                ok=False,
                skipped=True,
                fallback=adapter.fallback,
                details={"reason": "not configured"},
            ), None

        if (
            session_error is not None
            and adapter.session_policy != "none"
            and adapter.session_domain
        ):
            # The caller's session is unusable (e.g. expired): only the sources that would
            # have used it fail; the others still run.
            domain = str(session_error.details.get("domain") or "")
            if domain and (
                domain_matches(domain, adapter.session_domain)
                or domain_matches(adapter.session_domain, domain)
            ):
                status = _fail(
                    name, session_error.code, session_error.message, fallback=adapter.fallback
                )
                status.details = {"session_id": session_error.details.get("session_id")}
                return status, None

        usable_session = None
        if session is not None and adapter.session_policy != "none" and adapter.session_domain:
            if domain_matches(session.domain, adapter.session_domain) or domain_matches(
                adapter.session_domain, session.domain
            ):
                usable_session = session

        if adapter.session_policy == "required" and usable_session is None:
            message = (
                f"Source '{name}' requires an authenticated session for "
                f"{adapter.session_domain} (pass session_id)."
            )
            if explicit:
                code = (
                    ErrorCode.SESSION_REQUIRED
                    if session is None
                    else ErrorCode.SESSION_DOMAIN_MISMATCH
                )
                return _fail(name, code, message, fallback=adapter.fallback), None
            return SourceStatus(
                name=name,
                ok=False,
                skipped=True,
                fallback=adapter.fallback,
                details={"reason": "requires session", "session_domain": adapter.session_domain},
            ), None
        return None, usable_session

    async def _run_one(
        self, adapter: SourceAdapter, query: BaseQuery, session: SessionHandle | None
    ) -> _Run:
        started = time.perf_counter()
        timeout = adapter.timeout or self.settings.source_timeout
        if session is not None:
            timeout += self.settings.politeness_max_wait
        status: SourceStatus
        results: list[Result] = []
        ctx = self.context(session)
        try:
            raw = await asyncio.wait_for(adapter.search(query, ctx), timeout)
            for item in raw:
                if not isinstance(item, adapter.result_model):
                    raise ApiError(
                        ErrorCode.PARSE_ERROR,
                        f"Source returned {type(item).__name__}, expected "
                        f"{adapter.result_model.__name__}.",
                    )
            results = list(raw)
            status = SourceStatus(
                name=adapter.name, ok=True, count=len(results), fallback=adapter.fallback
            )
        except ApiError as exc:
            status = _fail(adapter.name, exc.code, exc.message, fallback=adapter.fallback)
            if exc.details:
                status.details = {k: v for k, v in exc.details.items() if k != "body"}
            if session is not None and exc.code in _SESSION_STATE_ERRORS and self.sessions:
                await self.sessions.record_failure(session.id, exc.code, exc.message)
        except TimeoutError:
            status = _fail(
                adapter.name,
                ErrorCode.SOURCE_TIMEOUT,
                f"Source '{adapter.name}' timed out after {timeout:.0f}s.",
                fallback=adapter.fallback,
            )
        except Exception as exc:
            logger.exception("Source %s failed", adapter.name)
            status = _fail(
                adapter.name,
                ErrorCode.SOURCE_ERROR,
                f"Source '{adapter.name}' failed: {type(exc).__name__}",
                fallback=adapter.fallback,
            )
        if ctx.details:
            status.details = {**ctx.details, **status.details}
        status.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _Run(adapter=adapter, status=status, results=results)

    async def _run_many(
        self,
        adapters: list[SourceAdapter],
        query: BaseQuery,
        session: SessionHandle | None,
        explicit: bool,
        session_error: ApiError | None = None,
    ) -> list[_Run]:
        runs: list[_Run | None] = [None] * len(adapters)
        tasks: dict[int, asyncio.Task[_Run]] = {}
        for index, adapter in enumerate(adapters):
            blocked, usable_session = self._preflight(adapter, explicit, session, session_error)
            if blocked is not None:
                runs[index] = _Run(adapter=adapter, status=blocked)
            else:
                tasks[index] = asyncio.create_task(self._run_one(adapter, query, usable_session))
        if tasks:
            await asyncio.gather(*tasks.values())
            for index, task in tasks.items():
                runs[index] = task.result()
        return [run for run in runs if run is not None]

    @staticmethod
    def _is_skipped(status: SourceStatus) -> bool:
        return status.skipped

    # ------------------------------------------------------------------ main entry
    async def search(
        self, vertical_name: str, query: BaseQuery, principal: Principal
    ) -> SearchResponse[Any]:
        started = time.perf_counter()
        vertical = self.registry.get(vertical_name)
        response_model = SearchResponse[vertical.result_model]  # type: ignore[name-defined]
        adapters = self.registry.sources_for(vertical_name)
        by_name = {a.name: a for a in adapters}
        mode = query.fallback or self.settings.search_fallback

        explicit = bool(query.sources)
        if explicit:
            unknown = [s for s in query.sources or [] if s not in by_name]
            if unknown:
                raise ApiError(
                    ErrorCode.SOURCE_UNKNOWN,
                    f"Unknown source(s): {', '.join(unknown)}.",
                    details={"unknown": unknown, "available": list(by_name)},
                )
            wanted = set(query.sources or [])
            primary = [a for a in adapters if a.name in wanted]
            fallbacks: list[SourceAdapter] = []
        else:
            primary = [a for a in adapters if a.default_enabled and not a.fallback]
            fallbacks = [a for a in adapters if a.default_enabled and a.fallback]
            if mode == "never":
                fallbacks = []

        session: SessionHandle | None = None
        session_error: ApiError | None = None
        if query.session_id:
            if self.sessions is None:
                raise ApiError(ErrorCode.SESSION_NOT_FOUND, "Sessions are not available.")
            try:
                session = await self.sessions.resolve(principal, query.session_id)
            except ApiError as exc:
                # Not found / not owned is a client error; an expired session only
                # affects the sources that need it.
                if exc.code != ErrorCode.SESSION_EXPIRED:
                    raise
                session_error = exc

        cache_key: str | None = None
        if not query.no_cache and session is None and session_error is None:
            cache_key = make_key(
                f"search:{vertical_name}",
                {"q": query.cache_payload(), "mode": mode, "sources": sorted(by_name)},
            )
            cached = await self.cache.get_model(cache_key, response_model)
            if cached is not None:
                cached.cached = True
                cached.took_ms = int((time.perf_counter() - started) * 1000)
                return cached

        if mode == "always" and not explicit:
            runs = await self._run_many(
                primary + fallbacks, query, session, explicit, session_error
            )
        else:
            runs = await self._run_many(primary, query, session, explicit, session_error)
            if mode == "auto" and fallbacks:
                attempted = [r for r in runs if not self._is_skipped(r.status)]
                succeeded = [r for r in attempted if r.status.ok]
                total = sum(len(r.results) for r in succeeded)
                if not succeeded or total < self.settings.search_fallback_min_results:
                    runs += await self._run_many(fallbacks, query, session, explicit=False)

        order = {a.name: i for i, a in enumerate(adapters)}
        runs.sort(key=lambda r: order.get(r.adapter.name, len(order)))
        ranked = [(r.adapter.name, r.results) for r in runs if r.status.ok]
        merged = merge_results(ranked, title_merge=vertical.title_merge)
        merged = vertical.postprocess(merged, query)
        merged = self._finalize(merged, query)

        statuses = [r.status for r in runs]
        failed = [s.name for s in statuses if not s.ok and not s.skipped]
        attempted_ok = any(s.ok for s in statuses)
        response = response_model(
            query={
                **query.model_dump(mode="json", exclude_none=True, exclude={"no_cache"}),
                "fallback": mode,
            },
            results=merged,
            count=len(merged),
            sources=statuses,
            failed_sources=failed,
            partial=bool(failed),
            cached=False,
            took_ms=int((time.perf_counter() - started) * 1000),
        )
        if cache_key and attempted_ok:
            ttl = (
                self.settings.cache_ttl_search
                if not failed
                else min(60, self.settings.cache_ttl_search)
            )
            await self.cache.set_model(cache_key, response, ttl)
        return response

    def _finalize(self, results: list[Result], query: BaseQuery) -> list[Result]:
        since, until = resolve_window(query.time_range, query.since, query.until)
        kept: list[Result] = []
        for result in results:
            if result.published_at is not None:
                if since and result.published_at < since:
                    continue
                if until and result.published_at > until:
                    continue
            if result.language is None:
                result.language = detect_language(
                    " ".join(filter(None, [result.title, result.snippet]))
                )
            kept.append(result)
        if query.sort == "date":
            kept.sort(
                key=lambda r: (
                    r.published_at is not None,
                    r.published_at.timestamp() if r.published_at else 0,
                ),
                reverse=True,
            )
        return kept[: query.limit]
