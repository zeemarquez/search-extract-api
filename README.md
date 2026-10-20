# search-extract-api

Open-source, self-hosted **search and extraction API** (FastAPI). It turns a **query**, a
**feed** or a **URL** into normalized results and clean, LLM-ready text — for any domain:
general web, news, jobs, housing and more. It also lets each user reach sites that require
logging in, with **their own session**, and offers optional AI endpoints to summarize,
deduplicate, classify and verify content.

It was designed as the internet gateway (“System A”) of an AI-personalized news feed
(“System B”), but any application can use it. Boundary rule: this service stores no user
data except the web sessions entrusted to it, and the client never scrapes directly — it
asks this API.

```
client (e.g. System B) ──HTTP──▶ search-extract-api ──▶ SearXNG / GDELT / Google News / feeds / websites
                                        │                 Crawl4AI (headless rendering)
                                        └──▶ any OpenAI-compatible LLM (optional)
```

## Features

| Area | Endpoints | Built on |
| --- | --- | --- |
| Web search | `GET/POST /search` — language, region and date filters | SearXNG (JSON), Tavily / Exa as optional fallbacks |
| Feeds | `GET /feeds`, `POST /feeds` (many at once), `GET /feeds/discover` | RSS / Atom / JSON Feed |
| Extraction | `GET/POST /extract`, `POST /extract/batch` — Markdown + metadata + content fingerprint | Crawl4AI, lightweight fallback (httpx + trafilatura) |
| Structured extraction | `POST /extract/structured` — URL or text + fields → JSON | Crawl4AI + LLM |
| AI | `POST /ai/summarize`, `/ai/dedup`, `/ai/classify`, `/ai/verify` | any OpenAI-compatible LLM; embeddings for dedup |
| Sessions | `/sessions` (cookie import, interactive login, health check, delete) | encrypted store + headless Chromium |
| News | `GET/POST /news/search`, `GET /news/top` | SearXNG news, GDELT DOC 2.0, Google News RSS |
| LinkedIn | `GET/POST /linkedin/jobs` (`session_id` required) | LinkedIn jobs with your own session |
| Idealista | `GET/POST /idealista/listings` | Idealista listings: official API with keys, website otherwise |
| Remotive | `GET/POST /remotive/jobs` | Remote jobs from remotive.com |
| Arbeitnow | `GET/POST /arbeitnow/jobs` | Jobs in Europe from arbeitnow.com |
| Service | `GET /health`, `GET /verticals`, `GET /<vertical>/sources`, OpenAPI at `/docs` | |

Design guarantees:

* **One result schema everywhere**, extended per vertical or provider (`NewsResult`,
  `JobResult` for the job providers, `HousingResult` for Idealista).
* **One endpoint per provider** for site-specific sources (`/linkedin`, `/idealista`,
  `/remotive`, `/arbeitnow`): each keeps its own filters and fields. When the provider
  blocks access (expired session, captcha, login wall) the endpoint answers with that
  error (HTTP 409) instead of an empty list.
* **Fault tolerant**: a failing source never fails the request; the response lists which sources failed and why.
* **Blocked pages are errors, never empty content**: `LOGIN_REQUIRED`, `SESSION_EXPIRED`, `CAPTCHA_REQUIRED`.
* **Stable content fingerprint**: the same article hashes the same even when ads, navigation or "updated 5 minutes ago" change — so a client can build reliable change alerts.
* **Pluggable**: sources and verticals are adapters/modules discovered through Python entry points; new ones need no core change ([docs/extending.md](docs/extending.md)).
* **Works without commercial keys**; everything is configured through environment variables with defaults that work on first start.

## Quick start (Docker, one command)

```bash
docker compose up -d
```

This starts the API on `http://127.0.0.1:8000`, SearXNG (JSON output enabled), Valkey
(cache), Crawl4AI (headless browser) and the encrypted session store (a Docker volume).
Interactive docs: <http://127.0.0.1:8000/docs>. Give Docker at least 4 GB of RAM: Crawl4AI
alone needs about 2 GB, and Docker Desktop's default 2 GB VM ends up swapping (raise it under
Settings → Resources, or in `.wslconfig` with the WSL 2 backend). `docker compose ps` should
show all four services as `healthy`; `/health?deep=true` also checks that the API reaches
SearXNG, Crawl4AI and the LLM (it reports `degraded` while no LLM is reachable).

```bash
curl "http://127.0.0.1:8000/search?q=open+source+llm&lang=en&time_range=week"
curl "http://127.0.0.1:8000/news/search?q=vivienda&lang=es&region=ES&time_range=day"
curl -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" -d '{"url": "https://example.com/article"}'
```

SearXNG's settings live in `deploy/searxng/` and are baked into a local image (no bind
mount), so after changing them run `docker compose build searxng && docker compose up -d`
(or redeploy).

To configure anything, copy `.env.example` to `.env` (all values optional). Before exposing
the API beyond localhost set `API_KEYS`, `API_BIND=0.0.0.0` and change `SEARXNG_SECRET` and
`CRAWL4AI_API_TOKEN`.

**Operational note:** SearXNG works best from a residential IP; several search engines block
data-center IPs. When SearXNG engines fail, `sources[].details.unresponsive_engines` says
which ones, and the commercial fallbacks (if configured) take over.

### Local LLM

The AI endpoints and structured extraction use any OpenAI-compatible endpoint. By default
the stack points at Ollama on the Docker host (`http://host.docker.internal:11434/v1`). To run
Ollama inside the stack instead, set `LLM_BASE_URL=http://ollama:11434/v1` in `.env` and add
the override file (it also pulls the chat and embeddings models on first start):

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d
```

The chat model (`LLM_CHAT_MODEL`, default `qwen2.5:7b-instruct`) and the embeddings model
(`EMBEDDINGS_MODEL`, default `bge-m3`, optional separate `EMBEDDINGS_BASE_URL`) are configured
independently, so you can mix a local embeddings model with a hosted chat model. Without an
LLM the AI endpoints answer `503 LLM_NOT_CONFIGURED` and `/ai/dedup` falls back to lexical
similarity; everything else keeps working.

### Without Docker

```bash
uv sync --extra browser               # or: pip install -e ".[browser]"
uv run playwright install chromium    # only needed for sessions' headless browser
SEARXNG_URL=http://localhost:8080 uv run search-extract-api --port 8000
```

## Using the API

The complete endpoint-by-endpoint guide, with examples and recipes, is in
[docs/user-guide.md](docs/user-guide.md). The essentials:

Authentication (when `API_KEYS` is set): `X-API-Key: <key>` or `Authorization: Bearer <key>`.
Rate limits apply per key (`RATE_LIMIT_PER_MINUTE`, headers `X-RateLimit-*`).

### Common result schema

```json
{
  "id": "4b1f0c2e9a7d3f61",
  "url": "https://elpais.com/economia/2026-10-18/plan-vivienda.html",
  "title": "El Gobierno aprueba el nuevo plan de vivienda",
  "source": "elpais.com",
  "published_at": "2026-10-18T07:40:00Z",
  "language": "es",
  "snippet": "El Consejo de Ministros aprobó…",
  "image": "https://imagenes.elpais.com/plan.jpg",
  "providers": ["searxng_news", "gdelt", "google_news"],
  "score": 0.04918,
  "extra": {"alternate_urls": ["https://news.google.com/rss/articles/…"]},
  "outlet": "El País",
  "author": null
}
```

* `url` is canonical (tracking parameters removed) and `id` is stable across URL variants
  (`www.`/`m.`/AMP/trailing slash), so clients can deduplicate by `id`.
* Google News results are matched to the same article from other sources by headline and
  outlet. Unmatched ones keep the Google redirect link unless you pass `resolve_links=true`
  (news search and `/news/top`), which resolves them to the publisher URL. `/extract`
  always resolves Google News links before fetching.
* `providers` lists every source that found the item. Results found by several sources are
  merged into one and ranked higher (reciprocal rank fusion).
* Verticals and providers add fields: news `outlet`, `author`; job providers `job_title`,
  `company`, `location`, `work_mode`, `employment_type`, `salary`; Idealista `operation`,
  `price`, `currency`,
  `area_m2`, `rooms`, `bathrooms`, `location`. `GET /verticals` returns every schema.

### Search envelope and fault tolerance

```json
{
  "query": {"q": "vivienda", "lang": "es", "fallback": "auto"},
  "results": [ ... ],
  "count": 18,
  "sources": [
    {"name": "searxng_news", "ok": true, "count": 12, "elapsed_ms": 840},
    {"name": "gdelt", "ok": false, "error": {"code": "SOURCE_TIMEOUT", "message": "…"}},
    {"name": "tavily_news", "ok": false, "skipped": true, "details": {"reason": "not configured"}}
  ],
  "failed_sources": ["gdelt"],
  "partial": true,
  "cached": false
}
```

Common filters on every search: `q`, `lang` (ISO 639-1), `region` (ISO 3166-1), `time_range`
(`day|week|month|year`) or `since`/`until`, `limit`, `page`, `sort` (`relevance|date`),
`sources` (comma-separated), `fallback` (`auto|always|never`), `session_id`, `no_cache`.
`GET` takes them as query parameters, `POST` as a JSON body.

### Extraction and change detection

```bash
curl -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" \
  -d '{"url": "https://www.boe.es/diario_boe/", "previous_fingerprint": "sha256:…"}'
```

Returns `markdown`, `title`, `author`, `published_at`, `language`, `image`, `site_name`, and
`fingerprint` (`hash`, `simhash`, `version`). With `previous_fingerprint` the response says
whether the main content `changed`; with `previous_simhash` you also get a `similarity`
(0–1) to ignore trivial edits. `mode` selects the fetcher (`auto`: Crawl4AI then the
lightweight fallback; the headless browser when a session is used). `content: "full"`
returns the whole page instead of the main content.

### AI endpoints

* `POST /ai/summarize` — `text` or `items` (results), `language`, `length` (`short|medium|long`) or `max_words`, `format` (`paragraph|bullets`).
* `POST /ai/dedup` — groups items describing the same thing (embeddings + cosine, lexical fallback).
* `POST /ai/classify` — `labels` from a given list (single or `multi_label`) or free labels.
* `POST /ai/verify` — *does this text meet this criterion?* → `matches`, `confidence`, `reason` and extracted `fields`; accepts `text` or a `url`.

## Authenticated sessions

Some sites (e.g. LinkedIn) only show content to logged-in users. Each user can let the API
use **their own session** without ever giving it their password.

1. **Import cookies** exported from the browser (Cookie-Editor / EditThisCookie JSON, a
   Playwright storage state, or Netscape `cookies.txt`):

   ```bash
   curl -X POST http://127.0.0.1:8000/sessions -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"domain": "linkedin.com", "cookies": [ ...exported cookies... ], "check": true}'
   ```

2. **Interactive login**: `POST /sessions/login {"domain": "linkedin.com"}` returns a
   `viewer_url`. The user opens it, sees a live remote browser and logs in there (2FA
   included); then presses *Save session*. Only the resulting session state is stored.

Then pass `session_id` to `/extract`, `/feeds`, `/ai/verify` or a vertical search
(`/linkedin/jobs?q=python&session_id=ses_…`).

* **Ownership**: a session belongs to its domain and to the API key that created it. Any
  other key gets `404 SESSION_NOT_FOUND`. (System B maps its users to their sessions.)
* **Security**: encrypted at rest (Fernet, `SESSION_ENCRYPTION_KEY`, rotation supported),
  never returned in responses, redacted from logs, deletable on demand (`DELETE /sessions/{id}`).
  Cookies are only sent to the session's domain and are never forwarded to other services.
* **Health**: an expired session, a login request or a captcha is returned as its own error
  (`SESSION_EXPIRED`, `CAPTCHA_REQUIRED`, HTTP 409) and recorded in the session status —
  never as an empty result. `POST /sessions/{id}/check` verifies a session on demand.
* **Prudent use**: requests with a session are paced like a human (minimum interval plus
  random jitter, hourly budget) and requests per domain are spaced too.

> **Risk:** automated access with a personal account may violate the terms of service of
> some websites and can lead to the account being restricted or banned. Each user decides
> whether to use this feature with their own account.

See [docs/sessions.md](docs/sessions.md) for details.

## Error codes

Errors always have the shape `{"error": {"code", "message", "details"}}`. Main codes:
`UNAUTHORIZED` (401), `RATE_LIMITED` (429), `VALIDATION_ERROR` (422), `INVALID_URL` /
`BLOCKED_URL` (400, private addresses are blocked), `FETCH_FAILED` (502), `FETCH_TIMEOUT`
(504), `UPSTREAM_HTTP_ERROR` (502, `details.status_code`), `UNSUPPORTED_CONTENT` (415),
`EXTRACTION_FAILED` (422), `LOGIN_REQUIRED` / `SESSION_EXPIRED` / `CAPTCHA_REQUIRED` (409),
`ACCESS_BLOCKED` (502, the site refuses automated access outright),
`SESSION_NOT_FOUND` (404), `SESSION_DOMAIN_MISMATCH` (400), `SESSION_RATE_LIMITED` (429),
`BROWSER_UNAVAILABLE` (503), `LLM_NOT_CONFIGURED` / `LLM_UNAVAILABLE` (503), `LLM_BAD_OUTPUT`
(502). Per-source errors inside search responses use `SOURCE_ERROR`, `SOURCE_TIMEOUT`,
`SOURCE_NOT_CONFIGURED`, `SOURCE_BLOCKED`, `PARSE_ERROR` and the codes above.

## Configuration

All settings are environment variables (or `.env`). The most relevant:

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_KEYS` | *(empty = no auth)* | Comma-separated keys, optionally `name:key` |
| `RATE_LIMIT_PER_MINUTE` | `120` | Requests per minute per key (0 = off) |
| `REDIS_URL` | *(memory)* | Redis/Valkey cache; falls back to memory if unreachable |
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG instance with JSON output |
| `CRAWL4AI_URL`, `CRAWL4AI_TOKEN` | *(off)* | Crawl4AI server; without it the light extractor is used |
| `TAVILY_API_KEY`, `EXA_API_KEY` | *(off)* | Commercial fallbacks |
| `SEARCH_FALLBACK` | `auto` | `auto`, `always` or `never` |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_CHAT_MODEL` | *(off)* | OpenAI-compatible chat endpoint |
| `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_MODEL` | chat endpoint, `bge-m3` | Embeddings for dedup |
| `SESSIONS_DB_PATH` | `./data/sessions.db` | Encrypted session store |
| `SESSION_ENCRYPTION_KEY` | *(auto-generated)* | Fernet key(s); comma-separated to rotate |
| `SESSION_MIN_INTERVAL`, `SESSION_JITTER`, `SESSION_MAX_REQUESTS_PER_HOUR` | `4`, `3`, `120` | Human-like pacing |
| `DOMAIN_MIN_INTERVAL` | `1` | Seconds between requests to the same site |
| `ENABLED_VERTICALS`, `DISABLED_SOURCES` | *(all)* | Turn verticals / sources off |
| `IDEALISTA_API_KEY`, `IDEALISTA_API_SECRET` | *(off)* | Official Idealista API |
| `ALLOW_PRIVATE_URLS` | `false` | Disable the SSRF guard (not recommended) |

See [`src/search_extract_api/config.py`](src/search_extract_api/config.py) for the full list.

## Development

```bash
uv sync                 # Python 3.11+
uv run pytest           # all tests (no network needed)
uv run pytest -m a1     # one phase: a1, a2, a3 or a4
uv run ruff check src tests
```

The project was built in four phases, each functional and tested before the next:

| Phase | Scope | Tests |
| --- | --- | --- |
| A1 | Core: web search, feeds, extraction with fingerprint, common schema, Docker Compose | `-m a1` |
| A2 | News vertical, AI endpoints (summarize, dedup, classify, verify), commercial fallbacks | `-m a2` |
| A3 | Authenticated sessions (cookie import, interactive login), structured extraction | `-m a3` |
| A4 | Job and real-estate providers (LinkedIn, Remotive, Arbeitnow, Idealista) | `-m a4` |

The suite covers the schema contract of every adapter and vertical, URL merging and
deduplication across sources, behavior when a source fails, fingerprint stability when ads
or page elements change, session ownership and expired-session errors.

## License

[MIT](LICENSE).
