# search-extract-api — User guide

This guide explains how to use every endpoint of the API: what to send, what you get back
and how to handle errors. For installation see the [README](../README.md); for adding
sources see [extending.md](extending.md); for sessions in depth see [sessions.md](sessions.md).

The interactive reference (try any endpoint from the browser) is always available at
`http://<host>:8000/docs`, and the machine-readable spec at `/openapi.json`.

## Contents

1. [Basics](#1-basics): base URL, authentication, rate limits, GET vs POST, errors
2. [Web search](#2-web-search): `/search`
3. [News](#3-news): `/news/search`, `/news/top`
4. [Provider endpoints](#4-provider-endpoints): `/linkedin/jobs`, `/idealista/listings`, `/remotive/jobs`, `/arbeitnow/jobs`
5. [Feeds](#5-feeds): `/feeds`, `/feeds/discover`
6. [Extraction](#6-extraction): `/extract`, `/extract/batch`, `/extract/structured`
7. [AI](#7-ai): `/ai/summarize`, `/ai/dedup`, `/ai/classify`, `/ai/verify`
8. [Sessions](#8-sessions): cookie import, interactive login, health
9. [Service](#9-service): `/health`, `/verticals`, `/<…>/sources`
10. [Recipes](#10-recipes): change alerts, a news pipeline, criteria alerts
11. [Error reference](#11-error-reference)

---

## 1. Basics

### Base URL

With Docker Compose the API listens on `http://127.0.0.1:8000` (set `API_BIND=0.0.0.0` to
expose it beyond the machine). All examples below use that address.

### Authentication

If the server has `API_KEYS` configured, send your key in either header:

```bash
curl -H "X-API-Key: sk-yourkey" "http://127.0.0.1:8000/search?q=python"
curl -H "Authorization: Bearer sk-yourkey" "http://127.0.0.1:8000/search?q=python"
```

Without `API_KEYS` the API is open (meant for a single user or a trusted network).
`/health`, `/docs` and `/openapi.json` never need a key.

Your API key also **owns** the sessions you create: no other key can see or use them.

### Rate limits

Each key may make `RATE_LIMIT_PER_MINUTE` requests per minute (120 by default). Every
response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`; over the limit you get
`429 RATE_LIMITED` with a `Retry-After` header.

Separately, requests to the **same website** are spaced out (1 s by default) and requests
that use a **session** are paced like a human (a few seconds plus random jitter, and an
hourly budget). These waits are handled for you; if a queue would wait too long you get
`429 DOMAIN_RATE_LIMITED` or `429 SESSION_RATE_LIMITED`.

### GET or POST

Every search endpoint accepts both forms with the same fields:

```bash
# Query parameters (lists are comma-separated)
curl "http://127.0.0.1:8000/news/search?q=vivienda&lang=es&sources=gdelt,google_news"

# JSON body
curl -X POST http://127.0.0.1:8000/news/search -H "Content-Type: application/json" \
  -d '{"q": "vivienda", "lang": "es", "sources": ["gdelt", "google_news"]}'
```

Unknown parameters are rejected (`422 VALIDATION_ERROR`), which catches typos.

### Errors

Every error has the same shape. Branch on `code`, which is stable; the message is for humans.

```json
{
  "error": {
    "code": "SESSION_EXPIRED",
    "message": "The session is no longer valid: the site asks to log in again.",
    "details": {"session_id": "ses_…", "reason": "redirected to a login page (/authwall)"}
  }
}
```

The full list is in the [error reference](#11-error-reference).

### Caching

Search, feed and extraction results are cached for a short time (10 min, 5 min and 1 h by
default). Responses say `"cached": true` when served from the cache. Add `no_cache=true` to
force a fresh fetch. Anything fetched with a session is never cached.

---

## 2. Web search

`GET|POST /search` — general web search through SearXNG (many engines at once), with
Tavily and Exa as optional fallbacks.

### Parameters (common to every search endpoint)

| Field | Default | Description |
| --- | --- | --- |
| `q` | — | Query text (required for `/search` and `/news/search`) |
| `lang` | | Language, ISO 639-1 (`es`, `en`…) |
| `region` | | Region, ISO 3166-1 alpha-2 (`ES`, `US`…) |
| `time_range` | | `day`, `week`, `month` or `year` |
| `since`, `until` | | ISO dates/datetimes, e.g. `2026-10-01` or `2026-10-01T08:00:00Z` |
| `limit` | 20 | 1–100 results |
| `page` | 1 | 1–10 |
| `sort` | `relevance` | `relevance` or `date` (newest first) |
| `sources` | all default sources | Restrict to some sources, e.g. `searxng,exa` |
| `fallback` | server setting (`auto`) | `auto`: use Tavily/Exa only if the free sources fail or return fewer than 3 results; `always`; `never` |
| `session_id` | | A session for sources that need one |
| `no_cache` | `false` | Skip the cache |

`/search` also takes `safesearch`: `0` off, `1` moderate (default), `2` strict.

Results dated outside `since`/`until`/`time_range` are dropped; undated results are kept.

### Example

```bash
curl "http://127.0.0.1:8000/search?q=euribor&lang=es&region=ES&time_range=week&limit=10"
```

```json
{
  "query": {"q": "euribor", "lang": "es", "region": "ES", "time_range": "week", "limit": 10, "fallback": "auto"},
  "results": [
    {
      "id": "8f2c41d09ab3e7c5",
      "url": "https://www.example.es/economia/euribor-octubre",
      "title": "El Euríbor baja en octubre",
      "source": "example.es",
      "published_at": "2026-10-18T07:40:00Z",
      "language": "es",
      "snippet": "El indicador hipotecario…",
      "image": "https://img.example.es/euribor.jpg",
      "providers": ["searxng"],
      "score": 0.0164,
      "extra": {"engines": ["bing", "duckduckgo"], "category": "general"}
    }
  ],
  "count": 1,
  "sources": [
    {"name": "searxng", "ok": true, "skipped": false, "count": 24, "elapsed_ms": 912,
     "fallback": false, "error": null, "details": {}}
  ],
  "failed_sources": [],
  "partial": false,
  "cached": false,
  "took_ms": 930
}
```

### The result schema

Every endpoint that returns results uses this schema; provider endpoints add fields to it.

| Field | Description |
| --- | --- |
| `id` | Stable id derived from the URL. The same document reached through `www.`, `m.`, AMP or a trailing slash gets the same id: deduplicate on it. |
| `url` | Canonical URL, tracking parameters (`utm_*`, `fbclid`…) removed |
| `title`, `snippet`, `image` | |
| `source` | Site that published it (`elpais.com`) |
| `published_at` | Publication date, UTC |
| `language` | ISO 639-1, detected when the source does not say |
| `providers` | Every source that found this result. Results found by several sources are merged and ranked higher. |
| `score` | Fused rank (higher is better) |
| `extra` | Source-specific data (engines, tags, alternate URLs…) |

### Reading the envelope

- **`sources`**: one entry per source with `ok`, `count`, `elapsed_ms` and, on failure,
  `error.code`/`error.message`.
- **`skipped: true`**: the source was not queried (e.g. a fallback without an API key).
  Skipped sources are not failures.
- **`failed_sources` / `partial`**: list the failures. A failing source **never fails the
  request**: you get the results of the others. Always check `partial` before concluding
  "there is nothing new".
- **`sources[].details.unresponsive_engines`**: which SearXNG engines did not answer
  (common on data-center IPs; SearXNG works best from a home connection).

---

## 3. News

### `GET|POST /news/search`

News from SearXNG's news engines, GDELT and Google News, merged into one list (with Tavily
and Exa as fallbacks). Takes the [common parameters](#parameters-common-to-every-search-endpoint) plus:

| Field | Description |
| --- | --- |
| `domains` | Only these outlets, e.g. `elpais.com,elmundo.es` |
| `exclude_domains` | Exclude these outlets |
| `resolve_links` | `true` to turn Google News redirect links into the publisher's URL (two extra requests to Google per link, cached) |

Sources: `searxng_news`, `gdelt`, `google_news`, `tavily_news`, `exa_news`.

Extra result fields: `outlet` (media name, e.g. "El País") and `author`.

```bash
curl "http://127.0.0.1:8000/news/search?q=huelga%20metro&lang=es&region=ES&time_range=day&sort=date"
```

Good to know:

- The same story found by several sources appears once, with all of them in `providers`.
- Google News results that could not be matched to another source keep a
  `news.google.com/rss/articles/…` link unless you pass `resolve_links=true`. `/extract`
  always resolves those links itself.
- GDELT is slow and rate-limits heavily; when it fails it shows up in `failed_sources`
  and the other sources still answer.

### `GET /news/top`

Top headlines for an edition, optionally for one topic.

| Field | Default | Description |
| --- | --- | --- |
| `lang`, `region` | `en`, edition default | e.g. `lang=es&region=ES` |
| `topic` | | `world`, `nation`, `business`, `technology`, `entertainment`, `sports`, `science`, `health` |
| `limit` | 30 | 1–100 |
| `resolve_links` | `false` | Publisher URLs instead of Google News links |
| `no_cache` | `false` | |

```bash
curl "http://127.0.0.1:8000/news/top?lang=es&region=ES&topic=business&resolve_links=true&limit=10"
```

---

## 4. Provider endpoints

Site-specific sources have one endpoint per provider. They take the
[common parameters](#parameters-common-to-every-search-endpoint) plus their own filters,
and return the common schema plus provider fields.

When the provider blocks access — expired session, captcha, login wall — these endpoints
answer with that error (**HTTP 409**, e.g. `SESSION_EXPIRED`, `CAPTCHA_REQUIRED`) instead of
an empty list, so "no results" always really means no results.

### Job providers: `/linkedin/jobs`, `/remotive/jobs`, `/arbeitnow/jobs`

| Field | Description |
| --- | --- |
| `q` | Keywords (title, skills) |
| `location` | City, region or country |
| `work_mode` | `onsite`, `remote` or `hybrid` |
| `company` | Company name |

At least one of `q`, `location` or `company` is required.

Extra result fields: `job_title`, `company`, `location`, `work_mode`
(`null` when the posting does not say), `employment_type`, `salary`; `published_at` is the
posting date.

**LinkedIn** (`GET|POST /linkedin/jobs`) needs your LinkedIn session (`session_id` is
required; see [Sessions](#8-sessions)). The login is verified at most every 30 minutes.

```bash
curl -H "X-API-Key: $KEY" \
  "http://127.0.0.1:8000/linkedin/jobs?q=python&location=Madrid&time_range=week&session_id=ses_abc123"
```

> Automated access with a personal account may break LinkedIn's terms of service and can
> get the account restricted. You decide whether to use it.

**Remotive** (`/remotive/jobs`): remote jobs. Remotive allows very few requests, so the API
downloads its list at most every 6 hours and filters locally; postings appear with a 24 h
delay. Results link to the Remotive page, as its terms require.

**Arbeitnow** (`/arbeitnow/jobs`): jobs in Europe, mostly Germany.

```bash
curl "http://127.0.0.1:8000/remotive/jobs?q=python&location=Europe"
curl "http://127.0.0.1:8000/arbeitnow/jobs?q=backend&location=Berlin"
```

### Idealista: `/idealista/listings`

Real-estate listings. Uses the official Idealista API when the server has
`IDEALISTA_API_KEY`/`IDEALISTA_API_SECRET`, and the website otherwise (Idealista's bot
protection often answers with a captcha; a session from a real browser helps).

| Field | Default | Description |
| --- | --- | --- |
| `operation` | `sale` | `sale` or `rent` |
| `property_type` | `homes` | `homes`, `rooms` (rent only), `offices`, `premises`, `garages`, `land` |
| `location` | | City name or Idealista URL slug (`madrid-madrid`, `chamberi-madrid`). Provincial capitals work by name. |
| `location_id`, `latitude` + `longitude` + `radius_m` | | For the official API |
| `min_price`, `max_price`, `min_area`, `max_area`, `min_rooms` | | Filters |
| `q` | | Words that must appear (e.g. `ático terraza`) |

One of `location`, `location_id` or `latitude`+`longitude` is required. The website reads
results from the requested page (`page`) and filters them locally.

Extra result fields: `operation`, `property_type`, `price` (monthly for rent), `currency`,
`area_m2`, `rooms`, `bathrooms`, `location`.

```bash
curl "http://127.0.0.1:8000/idealista/listings?operation=rent&location=madrid&max_price=1500&min_rooms=2"
```

---

## 5. Feeds

### `GET /feeds` — read one feed

```bash
curl "http://127.0.0.1:8000/feeds?url=https://feeds.bbci.co.uk/news/world/rss.xml&limit=20"
```

Parameters: `url` (required), `limit` (default 50), `session_id` (feeds behind a login),
`no_cache`. Works with RSS, Atom and JSON Feed.

Response: `feed` (title, link, language, format, item count…) and `results` in the common
schema. Each item has `providers: ["feed:<site>"]` and, in `extra`, `author`, `tags`,
`feed_url`, `feed_title`.

### `POST /feeds` — read many feeds at once

```bash
curl -X POST http://127.0.0.1:8000/feeds -H "Content-Type: application/json" -d '{
  "urls": ["https://feeds.bbci.co.uk/news/world/rss.xml", "https://www.theguardian.com/international/rss"],
  "limit_per_feed": 30,
  "limit": 100,
  "since": "2026-10-18T00:00:00Z"
}'
```

Items are merged, deduplicated and sorted newest first. As with searches, a feed that fails
is listed in `failed_sources` and the others are still returned.

### `GET /feeds/discover` — find a site's feeds

```bash
curl "http://127.0.0.1:8000/feeds/discover?url=https://www.theguardian.com/international"
```

Returns candidate feeds with how they were found (`link` tag, `anchor`, `guess` of common
paths, or `self` if the URL already is a feed) and, with `verify=true` (default), whether
they parse and how many items they have.

---

## 6. Extraction

### `GET|POST /extract` — a URL as clean Markdown

```bash
curl -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" \
  -d '{"url": "https://www.bbc.co.uk/news/articles/cxvgd0ymgzgyo"}'
```

| Field | Default | Description |
| --- | --- | --- |
| `url` | — | Page to extract (http/https). Google News links are resolved to the publisher first. |
| `mode` | `auto` | `auto` (Crawl4AI headless browser, then a lightweight fetcher; the embedded browser when a session is used), `crawl4ai`, `light` or `browser` |
| `content` | `main` | `main`: the article/main content only; `full`: the whole page |
| `session_id` | | Use an authenticated session |
| `max_chars` | 200 000 | Truncate the Markdown (`truncated: true` in the response) |
| `lang` | | Preferred language sent to the site |
| `include_links` | `false` | Also return the page's links |
| `previous_fingerprint`, `previous_simhash` | | Compare with an earlier extraction (see below) |
| `no_cache` | `false` | |

Response:

```json
{
  "url": "https://www.bbc.co.uk/news/articles/cxvgd0ymgzgyo",
  "requested_url": "https://www.bbc.co.uk/news/articles/cxvgd0ymgzgyo",
  "canonical_url": "https://www.bbc.co.uk/news/articles/cxvgd0ymgzgyo",
  "status_code": 200,
  "title": "Alberta heads to polls…",
  "author": "James FitzGerald",
  "published_at": "2026-10-19T09:24:33Z",
  "language": "en",
  "image": "https://ichef.bbci.co.uk/…jpg",
  "site_name": "BBC News",
  "markdown": "# Alberta heads to polls…\n\n**People in the province…",
  "word_count": 752,
  "fingerprint": {"hash": "sha256:f7477d2b…", "simhash": "4cb75ca5af4dbfab", "version": 1, "length": 765},
  "changed": null,
  "similarity": null,
  "fetcher": "crawl4ai",
  "extractor": "trafilatura",
  "cached": false
}
```

**The fingerprint.** `fingerprint.hash` is computed over the normalized main content only,
so it does **not** change when ads, menus, cookie banners, "related articles", tracking
parameters or "updated 5 minutes ago" change — only when the content itself does. Store it
and send it back as `previous_fingerprint` to get `changed: true|false`. Store
`fingerprint.simhash` too and send it as `previous_simhash` to get a `similarity` from 0 to 1
(≈1.0 identical, ≥0.9 small edit, ≈0.5 unrelated) to ignore trivial edits. Only compare
fingerprints with the same `version`.

**Blocked pages are errors, never content**: a login wall gives `409 LOGIN_REQUIRED` (or
`SESSION_EXPIRED` with a session), a bot challenge `409 CAPTCHA_REQUIRED`, a site refusing
automated access `502 ACCESS_BLOCKED`, a 404 `502 UPSTREAM_HTTP_ERROR` with
`details.status_code`. Addresses on private networks are refused (`400 BLOCKED_URL`).

### `POST /extract/batch` — several URLs

```bash
curl -X POST http://127.0.0.1:8000/extract/batch -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.com/a", "https://example.com/b"], "concurrency": 4}'
```

Up to 50 URLs; same options as `/extract`. Each item succeeds or fails on its own:
`items[].ok`, `items[].result` or `items[].error`, plus `succeeded`/`failed` counts.

### `POST /extract/structured` — fields as JSON

Give a URL (or text) and the fields you want; an LLM fills them from the page. Useful for
sites without a provider endpoint. Needs an LLM (see [AI](#7-ai)).

```bash
curl -X POST http://127.0.0.1:8000/extract/structured -H "Content-Type: application/json" -d '{
  "url": "https://www.example-shop.com/product/123",
  "fields": [
    {"name": "product", "type": "string"},
    {"name": "price", "type": "number", "description": "Current price in euros", "required": true},
    {"name": "in_stock", "type": "boolean"}
  ]
}'
```

```json
{"data": {"product": "Zapatillas X", "price": 59.9, "in_stock": true},
 "missing_fields": [], "url": "https://www.example-shop.com/product/123",
 "model": "qwen2.5:7b-instruct", "source_chars": 8123, "truncated": false, "fetcher": "crawl4ai"}
```

- Field types: `string`, `number`, `integer`, `boolean`, `date`, `array`, `object`.
- Instead of `fields` you can send a `json_schema`; add `instructions` to guide the model.
- Missing values come back as `null` (never invented) and required ones are listed in
  `missing_fields`.

---

## 7. AI

These endpoints need an OpenAI-compatible LLM configured on the server (`LLM_BASE_URL`,
e.g. Ollama). Without one they answer `503 LLM_NOT_CONFIGURED` (except `/ai/dedup`, which
falls back to lexical similarity). Answers are cached for 24 h.

Wherever a list of `items` is accepted you can pass search results as they are: `snippet`,
`content` and `markdown` are read as the item's text.

### `POST /ai/summarize`

```bash
curl -X POST http://127.0.0.1:8000/ai/summarize -H "Content-Type: application/json" -d '{
  "items": [{"title": "…", "snippet": "…", "source": "elpais.com"}, {"title": "…", "snippet": "…"}],
  "language": "es",
  "length": "short",
  "format": "bullets"
}'
```

Exactly one of `text` or `items`. `length`: `short` (~60 words), `medium` (~150), `long`
(~350), or an exact `max_words`. `format`: `paragraph` or `bullets`. `focus` steers the
summary ("impact on mortgages"). `language` defaults to the input's. Long inputs are
summarized in parts first (`chunks` in the response).

### `POST /ai/dedup`

Groups items that describe the same thing (e.g. the same story from several outlets).

```bash
curl -X POST http://127.0.0.1:8000/ai/dedup -H "Content-Type: application/json" -d '{
  "items": [
    {"id": "1", "title": "Huelga de metro en Madrid el lunes"},
    {"id": "2", "title": "Convocada huelga en el metro de Madrid para el lunes"},
    {"id": "3", "title": "El Real Madrid gana la liga"}
  ]
}'
```

```json
{"clusters": [{"id": "c1", "representative": "1", "items": ["1", "2"], "size": 2},
              {"id": "c2", "representative": "3", "items": ["3"], "size": 1}],
 "duplicates": 1, "method": "embeddings", "threshold": 0.86, "model": "bge-m3"}
```

`method`: `auto` (embeddings if configured, otherwise lexical), `embeddings` or `lexical`.
`threshold` (0–1) tunes how similar items must be; defaults are 0.86 for embeddings and 0.5
for lexical. Items with the same canonical URL are always grouped. The representative is
the first item of each group in your input order.

### `POST /ai/classify`

```bash
# Pick from your labels
curl -X POST http://127.0.0.1:8000/ai/classify -H "Content-Type: application/json" \
  -d '{"text": "El Euríbor sube por tercer mes", "labels": ["Economía", "Deportes", "Política"]}'

# Free labels, several items
curl -X POST http://127.0.0.1:8000/ai/classify -H "Content-Type: application/json" \
  -d '{"items": [{"id": "a", "text": "…"}, {"id": "b", "text": "…"}], "max_labels": 3, "language": "es"}'
```

Response: `results[]` (one per item, with its `id`) containing `labels[]` of
`{label, confidence}` ordered by confidence. With `labels` the answer is always one of
yours (one, or up to `max_labels` with `multi_label: true`); without `labels` the model
proposes up to `max_labels` short labels. `instructions` adds guidance.

### `POST /ai/verify` — does this text meet a criterion?

```bash
curl -X POST http://127.0.0.1:8000/ai/verify -H "Content-Type: application/json" -d '{
  "criterion": "Announces a public transport strike in Madrid",
  "url": "https://www.example.es/madrid/huelga-metro",
  "language": "es",
  "fields": [{"name": "date", "type": "date"}, {"name": "lines_affected", "type": "integer"}]
}'
```

```json
{"matches": true, "confidence": 0.87,
 "reason": "El texto anuncia una huelga en el metro de Madrid el lunes 20.",
 "fields": {"date": "2026-10-20", "lines_affected": 12},
 "model": "qwen2.5:7b-instruct", "url": "https://www.example.es/madrid/huelga-metro",
 "source_chars": 4210, "truncated": false}
```

Send `text` or a `url` (extracted first; `session_id` and `mode` apply). The model is
instructed to be strict: `matches` is only true when the text clearly supports the criterion.

---

## 8. Sessions

Some sites only show content when logged in. A session lets the API use **your** login
without ever knowing your password. Full details: [sessions.md](sessions.md).

### Import cookies

Export the cookies of a logged-in tab with a browser extension (Cookie-Editor JSON, or
`cookies.txt`) and send them:

```bash
curl -X POST http://127.0.0.1:8000/sessions -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{
  "domain": "linkedin.com",
  "label": "my LinkedIn",
  "cookies": [ … exported cookies … ],
  "check": true
}'
```

Returns the session metadata (never the cookies): `id` (`ses_…`), `domain`, `status`,
`cookie_count`, `expires_at`… Accepted formats: a JSON array of cookies, a Playwright
storage state, or `cookies.txt` text (`format` is detected automatically). Cookies for other
domains are dropped.

### Interactive login

```bash
curl -X POST http://127.0.0.1:8000/sessions/login -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{"domain": "linkedin.com"}'
```

```json
{"login_id": "lgn_…", "domain": "linkedin.com", "status": "pending",
 "viewer_url": "http://127.0.0.1:8000/sessions/login/lgn_…/view#token=…",
 "expires_at": "…"}
```

Open `viewer_url` in a browser: it shows a live remote browser. Log in there (2FA
included) and press **I'm logged in — save session**. The `viewer_url` contains a one-time
secret: give it only to the person logging in. Then:

| Request | Purpose |
| --- | --- |
| `GET /sessions/login/{login_id}` | Status: `pending`, `completed` (with `session`), `cancelled`, `expired` |
| `POST /sessions/login/{login_id}/complete` | Save the session from the API side instead of the button |
| `DELETE /sessions/login/{login_id}` | Cancel |

Pending logins expire after 10 minutes. Pass `session_id` when starting to refresh an
existing session instead of creating a new one.

### Manage sessions

| Request | Purpose |
| --- | --- |
| `GET /sessions` | Your sessions (metadata only) |
| `GET /sessions/{id}` | One session |
| `PATCH /sessions/{id}` | Replace its cookies (`cookies`, `format`) or change its `label` |
| `POST /sessions/{id}/check` | Verify it on the site: `active`, `expired`, `captcha` or `error` |
| `DELETE /sessions/{id}` | Delete it (204) |
| `GET /sessions/sites` | Sites with built-in support (login page, check page, login cookies) |

### Use a session

Add `session_id` to `/extract`, `/extract/batch`, `/extract/structured`, `/ai/verify`,
`/feeds`, `/linkedin/jobs` (required) or `/idealista/listings` (optional).

When the session stops working you get its own error, and the session's `status` is
updated so `GET /sessions` shows it:

| Error | Meaning | What to do |
| --- | --- | --- |
| `409 SESSION_EXPIRED` | Login cookies expired, or the site asks to log in again | Import fresh cookies or log in again |
| `409 CAPTCHA_REQUIRED` | The site shows a captcha / security check | Solve it in your browser, then refresh the session |
| `404 SESSION_NOT_FOUND` | Unknown id, or created by another API key | Use your own session |
| `400 SESSION_DOMAIN_MISMATCH` | The URL is not on the session's site | |
| `429 SESSION_RATE_LIMITED` | The session's hourly budget is used up | Wait |

> Automated access with a personal account can violate some sites' terms of service and
> lead to the account being restricted. Each user decides whether to use it.

---

## 9. Service

| Request | Purpose |
| --- | --- |
| `GET /health` | Quick status: `ok` or `degraded` (never fails because an optional component is missing) |
| `GET /health?deep=true` | Also probes SearXNG, Crawl4AI and the LLM |
| `GET /verticals` | Every search endpoint with its query schema, result schema and sources |
| `GET /search/sources`, `GET /news/sources`, `GET /<provider>/sources` | Sources of an endpoint: configured or not, fallback, session needs, notes |
| `GET /docs`, `GET /openapi.json` | Interactive and machine-readable reference |

---

## 10. Recipes

### Alert when a page changes

1. `POST /extract {"url": PAGE}` and store `fingerprint.hash` and `fingerprint.simhash`.
2. Periodically: `POST /extract {"url": PAGE, "no_cache": true, "previous_fingerprint": HASH,
   "previous_simhash": SIMHASH}`.
3. If `changed` is true and `similarity` is below your tolerance (e.g. 0.97), alert and
   store the new fingerprint.
4. On errors (`ACCESS_BLOCKED`, `CAPTCHA_REQUIRED`, `UPSTREAM_HTTP_ERROR`…), retry later:
   never treat an error as "unchanged".

### A personalized news pipeline

1. Collect: `GET /news/search` for each topic (with `time_range=day`), `POST /feeds` for the
   user's feeds, `GET /news/top` for headlines.
2. Merge client-side by result `id`; then `POST /ai/dedup` to group the same story told by
   different outlets.
3. `POST /extract` the representative of each group you want to show in full (or summarize
   the group with `POST /ai/summarize` and `items`).
4. `POST /ai/classify` with your topic labels to route stories.
5. Check `partial`/`failed_sources` in every response to know when coverage was incomplete.

### Alert when news meets a criterion

1. Search: `GET /news/search?q=huelga%20metro&lang=es&time_range=day&sort=date`.
2. For each new result: `POST /ai/verify {"criterion": "Announces a metro strike in Madrid",
   "url": RESULT_URL, "fields": [{"name": "date", "type": "date"}]}`.
3. Alert when `matches` is true and `confidence` is high enough (e.g. ≥ 0.8).

---

## 11. Error reference

| Code | HTTP | Meaning |
| --- | --- | --- |
| `UNAUTHORIZED` | 401 | Missing or wrong API key |
| `RATE_LIMITED` | 429 | Too many requests for your key (`Retry-After`) |
| `VALIDATION_ERROR` | 422 | Invalid or unknown parameters (`details.errors`) |
| `NOT_FOUND` | 404 | Unknown endpoint |
| `INVALID_URL` | 400 | Not an absolute http(s) URL |
| `BLOCKED_URL` | 400 | URL points to a private/local address |
| `SOURCE_UNKNOWN` | 400 | `sources` names a source that does not exist (`details.available`) |
| `FETCH_FAILED` | 502 | The page could not be fetched (DNS, connection, unresolvable link…) |
| `FETCH_TIMEOUT` | 504 | The site did not answer in time |
| `UPSTREAM_HTTP_ERROR` | 502 | The site answered with an error (`details.status_code`) |
| `UPSTREAM_RATE_LIMITED` | 502 | The site is rate limiting us (HTTP 429 from the site) |
| `ACCESS_BLOCKED` | 502 | The site refuses automated access or shows a cookie-consent wall |
| `CONTENT_TOO_LARGE` | 413 | Page larger than the server limit |
| `UNSUPPORTED_CONTENT` | 415 | Not HTML/text (e.g. PDF, image) |
| `EXTRACTION_FAILED` | 422 | No readable content on the page |
| `LOGIN_REQUIRED` | 409 | The page needs a login: create a session |
| `CAPTCHA_REQUIRED` | 409 | Captcha / bot challenge |
| `DOMAIN_RATE_LIMITED` | 429 | Too many queued requests for that site |
| `FEED_NOT_FOUND` | 404 | The feed URL does not exist |
| `FEED_PARSE_ERROR` | 422 | Not a valid feed (try `/feeds/discover`) |
| `SESSION_NOT_FOUND` | 404 | Unknown session or owned by another key |
| `SESSION_INVALID` | 400 | Cookies could not be parsed / none for the domain / undecryptable |
| `SESSION_EXPIRED` | 409 | The session no longer logs in |
| `SESSION_REQUIRED` | 400 | The source needs a session |
| `SESSION_DOMAIN_MISMATCH` | 400 | Session and URL are on different sites |
| `SESSION_RATE_LIMITED` | 429 | Session request budget used up |
| `LOGIN_NOT_FOUND` | 404 | Unknown interactive login |
| `LOGIN_EXPIRED` | 410 | The interactive login expired or was cancelled |
| `LOGIN_INCOMPLETE` | 409 | Saved before finishing the login (no login cookie yet) |
| `BROWSER_UNAVAILABLE` | 503 | The headless browser is not available on the server |
| `LLM_NOT_CONFIGURED` | 503 | No LLM configured |
| `LLM_UNAVAILABLE` | 503 | The LLM endpoint is down or erroring |
| `LLM_BAD_OUTPUT` | 502 | The model did not return valid output (after one retry) |

Inside search responses, per-source failures (`sources[].error.code`) also use
`SOURCE_ERROR`, `SOURCE_TIMEOUT`, `SOURCE_NOT_CONFIGURED`, `SOURCE_BLOCKED` and
`PARSE_ERROR` (the site's layout changed).
