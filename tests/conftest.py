from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from search_extract_api.config import Settings
from search_extract_api.core.security import Principal, owner_id_for_key
from search_extract_api.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
SEARXNG = "http://searxng.test"
LLM = "http://llm.test/v1"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_principal(key: str = "key-a") -> Principal:
    return Principal(name=key, owner_id=owner_id_for_key(key), authenticated=True)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        api_keys="",
        rate_limit_per_minute=0,
        redis_url=None,
        searxng_url=SEARXNG,
        crawl4ai_url=None,
        llm_base_url=None,
        embeddings_base_url=None,
        tavily_api_key=None,
        exa_api_key=None,
        idealista_api_key=None,
        idealista_api_secret=None,
        sessions_db_path=str(tmp_path / "sessions.db"),
        session_encryption_key=Fernet.generate_key().decode(),
        allow_private_urls=True,  # tests never resolve DNS
        domain_min_interval=0,
        session_min_interval=0,
        session_jitter=0,
        gdelt_min_interval=0,
        browser_enabled=False,
        search_fallback="auto",
    )


@pytest.fixture
def net() -> respx.MockRouter:
    """Every outgoing request must be mocked: tests never touch the network."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


_SSL_CONTEXT = ssl.create_default_context()  # loading CA certificates is slow; do it once


def outgoing_client() -> httpx.AsyncClient:
    """Same configuration as the service's own client, with a shared SSL context."""
    return httpx.AsyncClient(follow_redirects=False, verify=_SSL_CONTEXT, timeout=10)


@pytest.fixture
def make_client(settings: Settings) -> Callable[..., Any]:
    @asynccontextmanager
    async def _make(**overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
        app_settings = settings.model_copy(update=overrides) if overrides else settings
        outgoing = outgoing_client()
        app = create_app(app_settings, http_client=outgoing)
        async with outgoing, app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                client.app = app  # type: ignore[attr-defined]
                yield client

    return _make


@pytest.fixture
async def client(make_client: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client() as c:
        yield c


def searxng_payload(
    results: list[dict[str, Any]], unresponsive: list[list[str]] | None = None
) -> dict:
    return {
        "query": "q",
        "results": results,
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": unresponsive or [],
    }


def searxng_item(url: str, title: str, content: str = "", **extra: Any) -> dict[str, Any]:
    item = {
        "url": url,
        "title": title,
        "content": content,
        "engine": "duckduckgo",
        "engines": ["duckduckgo"],
        "score": 1.0,
        "category": "general",
        "publishedDate": None,
        "thumbnail": "",
    }
    item.update(extra)
    return item
