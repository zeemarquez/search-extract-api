# Authenticated sessions

Some websites only show content to logged-in users. search-extract-api lets each user use
**their own session** for those sites. It never asks for, stores or logs passwords.

> **Risk notice.** Automated access with a personal account can violate the terms of
> service of some websites (LinkedIn and Idealista, for example) and may get the account
> restricted or banned. The service paces requests like a human, but it cannot make
> automation acceptable to a site that forbids it. Each user decides whether to use this
> feature with their account.

## Providing a session

### Option 1 — import cookies

Export the cookies of a logged-in browser tab with an extension such as *Cookie-Editor*
(JSON) or *Get cookies.txt LOCALLY* (Netscape format), then:

```bash
curl -X POST http://127.0.0.1:8000/sessions \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{
        "domain": "linkedin.com",
        "label": "Ana — LinkedIn",
        "cookies": [ { "domain": ".linkedin.com", "name": "li_at", "value": "…", "path": "/", "expirationDate": 1793000000, "secure": true, "httpOnly": true } ],
        "check": true
      }'
```

Accepted formats (`format`: `auto` by default):

* JSON array of cookies (Cookie-Editor, EditThisCookie…),
* Playwright storage state (`{"cookies": [...], "origins": [...]}`) — also keeps localStorage,
* Netscape `cookies.txt` as a string.

Cookies that do not belong to `domain` are dropped. To refresh a session later, send new
cookies with `PATCH /sessions/{id}`.

### Option 2 — interactive login

```bash
curl -X POST http://127.0.0.1:8000/sessions/login -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{"domain": "linkedin.com"}'
```

```json
{
  "login_id": "lgn_…",
  "status": "pending",
  "viewer_url": "https://api.example.com/sessions/login/lgn_…/view#token=…",
  "expires_at": "…"
}
```

Give `viewer_url` to the user (it is a one-time secret: the token lives in the URL fragment,
so it never reaches server logs). The page shows the remote headless browser; the user logs
in there — including 2FA — and presses **I'm logged in — save session**. The client can also
poll `GET /sessions/login/{login_id}` or call `POST /sessions/login/{login_id}/complete`.
Only the resulting storage state is encrypted and stored. Pending logins expire after
`LOGIN_TIMEOUT_SECONDS` (10 minutes by default). Set `PUBLIC_BASE_URL` (behind HTTPS) when
users reach the API through a proxy.

To refresh an existing session through the same flow, pass `"session_id"` when starting.

## Using a session

Pass `session_id` to:

* `POST /extract` and `/extract/batch` — rendered in the embedded headless browser with the
  full storage state (falls back to plain HTTP with the cookies),
* `GET /feeds` — private feeds,
* `POST /ai/verify` and `/extract/structured` with a `url`,
* provider endpoints that use one: `GET /linkedin/jobs?q=python&session_id=…` (required)
  and `GET /idealista/listings?location=madrid&session_id=…` (optional, fewer captchas).

Session cookies are only sent to the session's domain (and its subdomains), never to other
hosts and never to other services such as Crawl4AI.

## Ownership

A session belongs to one domain and to the API key that created it (stored as a one-way
hash). Only that key can list, use, check, update or delete it; for any other key it does
not exist (`404 SESSION_NOT_FOUND`). A client that serves many users with one API key (like
System B) keeps the user → session mapping on its side.

## Health and errors

| Situation | Result |
| --- | --- |
| Login cookies expired (by date) | `409 SESSION_EXPIRED`, status `expired` |
| Site redirects to its login page / shows a logged-out page | `409 SESSION_EXPIRED`, status `expired` |
| Captcha or bot challenge | `409 CAPTCHA_REQUIRED`, status `captcha` |
| Hourly budget for the session exhausted | `429 SESSION_RATE_LIMITED` |
| URL outside the session's domain | `400 SESSION_DOMAIN_MISMATCH` |

In multi-source searches these appear in `sources[].error` for the affected source while the
other sources still answer. A blocked page is never returned as an empty result.

`POST /sessions/{id}/check` loads a page that only renders for logged-in users and returns
`active`, `expired`, `captcha` or `error`. `GET /sessions` shows each session's `status`,
`status_reason`, `last_used_at`, `last_checked_at` and the earliest login-cookie expiry.

## Pacing

Requests that use a session wait at least `SESSION_MIN_INTERVAL` seconds plus a random
`SESSION_JITTER` between them, and each session has an hourly budget
(`SESSION_MAX_REQUESTS_PER_HOUR`). Independently, requests to the same site are spaced by
`DOMAIN_MIN_INTERVAL`. When the queue for a site or session would wait longer than
`POLITENESS_MAX_WAIT`, the request fails with a rate-limit error instead of piling up.

## Storage and security

* Sessions live in a SQLite file (`SESSIONS_DB_PATH`, the `sessions` volume in Docker),
  encrypted with Fernet (AES-128-CBC + HMAC-SHA256).
* Set `SESSION_ENCRYPTION_KEY` in production. Generate a key with
  `search-extract-api --generate-key`. To rotate, put the new key first:
  `SESSION_ENCRYPTION_KEY=new,old`. If unset, a key is generated on first start and stored
  next to the database — convenient, but anyone with the volume can then decrypt it.
* Session contents are never returned by the API and cookie values are redacted from logs.
* `DELETE /sessions/{id}` removes a session immediately.
