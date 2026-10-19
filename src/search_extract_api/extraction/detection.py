"""Detection of login walls, expired sessions and captcha / bot challenges.

A blocked page must never be returned as if it were content: callers turn a
:class:`Detection` into ``LOGIN_REQUIRED``, ``SESSION_EXPIRED`` or
``CAPTCHA_REQUIRED`` errors.

Signals are split into *strong* ones (a challenge page, a redirect to a login URL,
site-specific "logged out" markers) and *weak* ones (a password field or captcha
widget) that only count when the page has little real content — many normal pages
embed a reCAPTCHA in a newsletter form or a login box in the header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from ..sessions.profiles import DomainProfile, profile_for


class BlockKind(StrEnum):
    LOGIN = "login"
    CAPTCHA = "captcha"
    #: The site refuses automated access outright (no challenge a human could solve).
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Detection:
    kind: BlockKind
    reason: str


_STRONG_CAPTCHA = [
    re.compile(p, re.I)
    for p in (
        r"challenges\.cloudflare\.com/(?:turnstile|cdn-cgi)",
        r"/cdn-cgi/challenge-platform/",
        r"cf[-_]chl[-_](?:opt|bypass|captcha|widget)",
        r"<title>\s*(?:just a moment\.\.\.|attention required! \| cloudflare|un momento\.\.\.)",
        r"captcha-delivery\.com",
        r"\bdd\s*=\s*\{\s*['\"]?rt['\"]?\s*:",
        r"px-captcha|_pxCaptcha|perimeterx\.net/.+captcha",
        r"/sorry/index\?continue=",
        r"arkoselabs\.com|funcaptcha",
    )
]
# Only meaningful on pages without real content (an article may mention these words).
_WEAK_CAPTCHA = [
    re.compile(p, re.I)
    for p in (
        r"g-recaptcha",
        r"google\.com/recaptcha",
        r"h-captcha|hcaptcha\.com",
        r"cf-turnstile",
        r"<title>\s*(?:access denied|pardon our interruption|robot or human\?)",
        r"(?:please )?verify (?:that )?you are (?:a )?human",
        r"verifica(?:r)? que (?:eres|no eres) (?:un )?(?:humano|robot)",
        r"are you a robot\?",
    )
]
_LOGIN_PATHS = [
    re.compile(p, re.I)
    for p in (
        r"/(?:log[-_]?in|sign[-_]?in|signin|logon|authwall|auth/login|accounts?/login)(?:/|$|\.|\?)",
        r"/uas/login",
        r"/session/new",
        r"/oauth2?/authorize",
        r"/sso/",
        r"/checkpoint/lg/",
        r"/identity/login",
        r"/u/login",
    )
]
_CHALLENGE_PATHS = [
    re.compile(p, re.I) for p in (r"/checkpoint/challenge", r"/challenge/", r"/captcha")
]
_PASSWORD_INPUT = re.compile(r"<input[^>]+type=[\"']?password", re.I)
# Cookie-consent interstitials that replace the requested page (mostly for EU visitors).
_CONSENT_HOSTS = ("consent.google.", "consent.youtube.com", "consent.yahoo.com", "guce.yahoo.com")

THIN_CONTENT_CHARS = 600


def _is_login_url(url: str) -> bool:
    path = urlsplit(url).path or "/"
    return any(p.search(path) for p in _LOGIN_PATHS)


def detect_block(
    *,
    requested_url: str,
    final_url: str,
    status_code: int,
    html: str,
    main_text_chars: int | None = None,
    profile: DomainProfile | None = None,
) -> Detection | None:
    """Returns why the page is blocked, or ``None`` if it looks like real content."""
    profile = profile or profile_for(urlsplit(final_url or requested_url).hostname or "")
    head = html[:200_000] if html else ""
    thin = main_text_chars is not None and main_text_chars < THIN_CONTENT_CHARS
    final_path = urlsplit(final_url).path or "/"

    final_host = (urlsplit(final_url).hostname or "").lower()
    if (
        final_host.startswith(_CONSENT_HOSTS)
        and final_host != (urlsplit(requested_url).hostname or "").lower()
    ):
        return Detection(BlockKind.BLOCKED, f"redirected to a cookie-consent page ({final_host})")

    # Captcha / bot walls.
    for pattern in [*_STRONG_CAPTCHA, *profile.compiled_captcha()]:
        if pattern.search(head):
            return Detection(BlockKind.CAPTCHA, f"bot challenge detected ({pattern.pattern[:40]})")
    if any(p.search(final_path) for p in _CHALLENGE_PATHS) and final_url != requested_url:
        return Detection(BlockKind.CAPTCHA, f"redirected to a challenge page ({final_path})")
    if thin and any(p.search(head) for p in _WEAK_CAPTCHA):
        return Detection(BlockKind.CAPTCHA, "captcha widget on a page without content")
    if status_code in (403, 429, 503) and thin:
        if re.search(r"captcha", head, re.I):
            return Detection(BlockKind.CAPTCHA, f"HTTP {status_code} captcha page")
        if re.search(r"robot|\bbots?\b|crawl|automated|scrap", head, re.I):
            return Detection(
                BlockKind.BLOCKED, f"HTTP {status_code}: the site blocks automated access"
            )

    # Login walls.
    if not _is_login_url(requested_url):
        if final_url != requested_url and _is_login_url(final_url):
            return Detection(BlockKind.LOGIN, f"redirected to a login page ({final_path})")
        for pattern in profile.compiled_login_paths():
            if pattern.search(final_path) and final_url != requested_url:
                return Detection(BlockKind.LOGIN, f"redirected to a login page ({final_path})")
        for pattern in profile.compiled_logged_out():
            if pattern.search(head):
                return Detection(BlockKind.LOGIN, "the site shows a logged-out page")
        if status_code == 401:
            return Detection(BlockKind.LOGIN, "HTTP 401 from the site")
        if thin and _PASSWORD_INPUT.search(head):
            return Detection(BlockKind.LOGIN, "login form on a page without content")
    return None
