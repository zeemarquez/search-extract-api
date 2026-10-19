"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .ai.llm import LLMClient
from .ai.service import AIService
from .config import Settings, get_settings
from .core.cache import Cache
from .core.http import HttpService
from .core.security import RedactingFilter
from .errors import install_error_handlers
from .extraction.service import ExtractionService
from .feeds.service import FeedService
from .routers import ai as ai_router
from .routers import extract as extract_router
from .routers import feeds as feeds_router
from .routers import health as health_router
from .routers import sessions as sessions_router
from .services.search import SearchService
from .sessions.browser import BrowserManager
from .sessions.login import LoginManager
from .sessions.profiles import register_profile
from .sessions.service import SessionService
from .sessions.store import SessionCrypto, SessionStore
from .verticals.base import Vertical, VerticalRegistry
from .verticals.router import build_vertical_router

logger = logging.getLogger("search_extract_api")

DESCRIPTION = """
Open-source, self-hosted API that turns a **query**, a **feed** or a **URL** into
normalized results and clean, LLM-ready text — for any domain: general web, news,
jobs, housing and more.

* **Search** (`/search`) and vertical searches (`/news`, `/jobs`, `/housing`...), merged and
  deduplicated across sources. A failing source never fails the request.
* **Feeds** (`/feeds`): RSS/Atom/JSON Feed reading and discovery.
* **Extraction** (`/extract`): clean Markdown + metadata + a stable content fingerprint.
* **Sessions** (`/sessions`): use sites that require logging in, with your own session.
* **AI** (`/ai`): summarize, deduplicate, classify and verify (optional, needs an LLM).

Authentication: `X-API-Key: <key>` or `Authorization: Bearer <key>` (when API keys are
configured). Errors always look like `{"error": {"code", "message", "details"}}`.
"""

TAGS = [
    {"name": "Web search", "description": "General web search (SearXNG + optional fallbacks)."},
    {"name": "Feeds", "description": "RSS / Atom / JSON Feed reading and discovery."},
    {"name": "Extraction", "description": "URL to clean Markdown, metadata and fingerprint."},
    {"name": "AI", "description": "Optional LLM-powered endpoints."},
    {"name": "Sessions", "description": "Authenticated sessions (cookies or interactive login)."},
    {"name": "Service", "description": "Health and discovery."},
]


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("search_extract_api").setLevel(level)
    redactor = RedactingFilter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "search_extract_api"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(redactor)
    # httpx logs full request URLs at INFO; keep them out of the default logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    extra_verticals: list[Vertical] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    registry = VerticalRegistry.load(settings, extra=extra_verticals or [])
    for profile in registry.all_profiles():
        register_profile(profile)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cache = await Cache.create(settings.redis_url, max_items=settings.cache_max_memory_items)
        http = HttpService(settings, cache, client=http_client)
        crypto = SessionCrypto.from_settings(
            settings.session_encryption_key, settings.sessions_db_path
        )
        store = SessionStore(settings.sessions_db_path, crypto)
        sessions = SessionService(settings=settings, store=store, http=http)
        browser = BrowserManager(settings)
        extraction = ExtractionService(
            settings=settings,
            http=http,
            cache=cache,
            browser=browser,
            sessions=sessions,
            resolvers=registry.all_resolvers(),
        )
        sessions.extraction = extraction
        llm = LLMClient(settings, http.client, cache)
        state = app.state
        state.cache = cache
        state.http = http
        state.sessions = sessions
        state.browser = browser
        state.extraction = extraction
        state.llm = llm
        state.ai = AIService(settings, llm, extraction)
        state.feeds = FeedService(settings=settings, http=http, cache=cache, sessions=sessions)
        state.search = SearchService(
            settings=settings,
            http=http,
            cache=cache,
            registry=registry,
            sessions=sessions,
            extraction=extraction,
        )
        state.logins = LoginManager(settings, browser, sessions)
        logger.info(
            "search-extract-api %s ready (auth=%s, cache=%s, searxng=%s, crawl4ai=%s, llm=%s, browser=%s)",
            __version__,
            "on" if settings.auth_enabled else "off",
            cache.backend_name,
            "on" if settings.searxng_url else "off",
            "on" if settings.crawl4ai_url else "off",
            "on" if llm.chat_configured else "off",
            "on" if browser.enabled else "off",
        )
        try:
            yield
        finally:
            await state.logins.close()
            await browser.close()
            if http_client is None:
                await http.aclose()
            await cache.close()

    app = FastAPI(
        title="search-extract-api",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=TAGS,
        license_info={"name": "MIT", "identifier": "MIT"},
    )
    app.state.settings = settings
    app.state.registry = registry
    install_error_handlers(app)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health_router.router)
    for vertical in registry.verticals.values():
        app.include_router(build_vertical_router(vertical))
    app.include_router(feeds_router.router)
    app.include_router(extract_router.router)
    app.include_router(ai_router.router)
    app.include_router(sessions_router.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, object]:
        return {
            "name": "search-extract-api",
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
            "verticals": sorted(registry.verticals),
        }

    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory search_extract_api.main:app_factory``."""
    return create_app()
