"""Service configuration.

Everything is configured through environment variables (or a ``.env`` file). Every
setting has a default that works on first start: without Redis the cache falls back
to memory, without Crawl4AI extraction falls back to the lightweight extractor,
without an LLM the AI endpoints answer ``503 LLM_NOT_CONFIGURED`` and so on.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # --- Service -------------------------------------------------------------------
    app_name: str = "search-extract-api"
    environment: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    public_base_url: str | None = Field(
        default=None,
        description="Public URL of this API, used to build interactive-login viewer links.",
    )
    cors_origins: str = Field(default="", description="Comma-separated list of allowed origins.")

    # --- Auth & rate limiting --------------------------------------------------------
    api_keys: str = Field(
        default="",
        description="Comma-separated API keys. Entries may be named as 'name:key'. "
        "Empty = authentication disabled (single-user / trusted network).",
    )
    rate_limit_per_minute: int = Field(default=120, ge=0, description="Per API key. 0 disables.")

    # --- Cache ---------------------------------------------------------------------
    redis_url: str | None = Field(
        default=None, description="redis:// URL of Redis or Valkey. Empty = in-memory cache."
    )
    cache_ttl_search: int = 600
    cache_ttl_feeds: int = 300
    cache_ttl_extract: int = 3600
    cache_ttl_ai: int = 86400
    cache_ttl_embeddings: int = 7 * 86400
    cache_max_memory_items: int = 5000

    # --- Outgoing HTTP ----------------------------------------------------------------
    http_timeout: float = 20.0
    source_timeout: float = 25.0
    user_agent: str = DEFAULT_BROWSER_UA
    api_user_agent: str = "search-extract-api/0.1 (+https://github.com/search-extract-api)"
    max_fetch_bytes: int = 10_000_000
    max_redirects: int = 8
    allow_private_urls: bool = Field(
        default=False,
        description="Allow fetching loopback/private/link-local addresses (SSRF guard off).",
    )

    # --- Politeness: per-domain and per-session pacing that mimics human use ------------
    domain_min_interval: float = Field(default=1.0, ge=0)
    session_min_interval: float = Field(default=4.0, ge=0)
    session_jitter: float = Field(default=3.0, ge=0)
    session_max_requests_per_hour: int = Field(default=120, ge=0)
    politeness_max_wait: float = Field(default=30.0, ge=0)

    # --- Web search ------------------------------------------------------------------
    searxng_url: str | None = "http://localhost:8080"
    searxng_max_pages: int = Field(default=3, ge=1, le=10)
    search_default_language: str | None = None
    search_fallback: Literal["auto", "always", "never"] = "auto"
    search_fallback_min_results: int = 3
    tavily_api_key: str | None = None
    exa_api_key: str | None = None

    # --- Extraction ------------------------------------------------------------------
    crawl4ai_url: str | None = None
    crawl4ai_token: str | None = Field(
        default=None, validation_alias=AliasChoices("crawl4ai_token", "crawl4ai_api_token")
    )
    crawl4ai_timeout: float = 60.0
    extract_default_mode: Literal["auto", "crawl4ai", "light", "browser"] = "auto"
    extract_max_chars: int = 200_000

    # --- Headless browser (interactive login, session rendering) ------------------------
    browser_enabled: bool = True
    browser_headless: bool = True
    browser_page_timeout: float = 45.0
    login_timeout_seconds: int = 600
    login_frame_interval: float = 0.4

    # --- LLM (any OpenAI-compatible endpoint: Ollama, vLLM, OpenAI, ...) ----------------
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_chat_model: str = "qwen2.5:7b-instruct"
    llm_timeout: float = 120.0
    llm_max_input_chars: int = 24_000
    llm_concurrency: int = Field(default=4, ge=1)
    embeddings_base_url: str | None = None
    embeddings_api_key: str | None = None
    embeddings_model: str = "bge-m3"
    embeddings_batch_size: int = 64

    # --- Sessions ----------------------------------------------------------------------
    sessions_db_path: str = "./data/sessions.db"
    session_encryption_key: str | None = Field(
        default=None,
        description="Fernet key(s), comma-separated for rotation (first = primary). "
        "Empty = generate one on first start and store it next to the database.",
    )

    # --- Verticals & sources -----------------------------------------------------------
    enabled_verticals: str = Field(default="", description="Comma-separated. Empty = all.")
    disabled_sources: str = Field(default="", description="Comma-separated source names.")
    gdelt_enabled: bool = True
    gdelt_min_interval: float = 5.0
    google_news_enabled: bool = True
    idealista_api_key: str | None = None
    idealista_api_secret: str | None = None

    @field_validator(
        "searxng_url",
        "crawl4ai_url",
        "llm_base_url",
        "embeddings_base_url",
        "redis_url",
        "public_base_url",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in {"none", "off", "disabled", "false"}:
                return None
            return value.rstrip("/")
        return value

    # --- Derived helpers ------------------------------------------------------------------
    @property
    def api_key_map(self) -> dict[str, str]:
        """Maps API key -> display name."""
        keys: dict[str, str] = {}
        for index, entry in enumerate(_split_csv(self.api_keys)):
            name, sep, key = entry.partition(":")
            if not sep:
                key, name = entry, f"key{index + 1}"
            keys[key.strip()] = name.strip() or f"key{index + 1}"
        return keys

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key_map)

    @property
    def cors_origin_list(self) -> list[str]:
        return _split_csv(self.cors_origins)

    @property
    def enabled_vertical_list(self) -> list[str]:
        return [v.lower() for v in _split_csv(self.enabled_verticals)]

    @property
    def disabled_source_list(self) -> set[str]:
        return {s.lower() for s in _split_csv(self.disabled_sources)}

    @property
    def effective_embeddings_base_url(self) -> str | None:
        return self.embeddings_base_url or self.llm_base_url

    @property
    def effective_embeddings_api_key(self) -> str | None:
        return self.embeddings_api_key or self.llm_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
