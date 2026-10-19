"""Cookie model, import formats (JSON exports, Playwright storage state, cookies.txt)
and RFC 6265-style matching used to attach session cookies to outgoing requests."""

from __future__ import annotations

import json
import time
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ..errors import ApiError, ErrorCode
from .urls import domain_matches

SameSite = Literal["Strict", "Lax", "None"]


class Cookie(BaseModel):
    """A cookie in Playwright's storage-state format."""

    name: str
    value: str
    domain: str = Field(description="'.example.com' for domain cookies, 'host' for host-only.")
    path: str = "/"
    expires: float = Field(default=-1, description="Epoch seconds; -1 for session cookies.")
    httpOnly: bool = False
    secure: bool = False
    sameSite: SameSite = "Lax"

    def is_expired(self, now: float | None = None) -> bool:
        return self.expires > 0 and self.expires < (now or time.time())

    def matches(self, url: str, now: float | None = None) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host or self.is_expired(now):
            return False
        if self.secure and parts.scheme != "https":
            return False
        domain = self.domain.lower()
        if domain.startswith("."):
            if not domain_matches(host, domain[1:]):
                return False
        elif host != domain:
            return False
        return _path_matches(parts.path or "/", self.path or "/")


class OriginStorage(BaseModel):
    origin: str
    localStorage: list[dict[str, str]] = Field(default_factory=list)


class StorageState(BaseModel):
    """Playwright-compatible storage state: what a session actually contains."""

    cookies: list[Cookie] = Field(default_factory=list)
    origins: list[OriginStorage] = Field(default_factory=list)

    def cookie_header(self, url: str) -> str | None:
        now = time.time()
        matching = [c for c in self.cookies if c.matches(url, now)]
        if not matching:
            return None
        matching.sort(key=lambda c: len(c.path or "/"), reverse=True)
        return "; ".join(f"{c.name}={c.value}" for c in matching)

    def filter_domain(self, domain: str) -> StorageState:
        cookies = [c for c in self.cookies if domain_matches(c.domain.lstrip("."), domain)]
        origins = [
            o for o in self.origins if domain_matches(urlsplit(o.origin).hostname or "", domain)
        ]
        return StorageState(cookies=cookies, origins=origins)

    def earliest_expiry(self, names: tuple[str, ...] = ()) -> float | None:
        relevant = [c for c in self.cookies if c.expires > 0 and (not names or c.name in names)]
        return min((c.expires for c in relevant), default=None)


def _path_matches(request_path: str, cookie_path: str) -> bool:
    if request_path == cookie_path:
        return True
    if request_path.startswith(cookie_path):
        return cookie_path.endswith("/") or request_path[len(cookie_path)] == "/"
    return False


def _same_site(value: Any) -> SameSite:
    text = str(value or "").strip().lower()
    if text in {"strict"}:
        return "Strict"
    if text in {"none", "no_restriction"}:
        return "None"
    return "Lax"


def _from_json_cookie(raw: dict[str, Any]) -> Cookie:
    name = raw.get("name") or raw.get("Name")
    value = raw.get("value", raw.get("Value"))
    domain = raw.get("domain") or raw.get("Domain") or raw.get("host")
    if not name or value is None or not domain:
        raise ValueError("cookie entries need name, value and domain")
    domain = str(domain).strip().lower()
    host_only = raw.get("hostOnly")
    if host_only is False and not domain.startswith("."):
        domain = "." + domain
    expires = raw.get("expires", raw.get("expirationDate", raw.get("expiry")))
    if raw.get("session") is True or expires in (None, "", "Session"):
        expires = -1
    try:
        expires_f = float(expires)
    except (TypeError, ValueError):
        expires_f = -1
    if expires_f > 1e12:  # milliseconds
        expires_f /= 1000
    return Cookie(
        name=str(name),
        value=str(value),
        domain=domain,
        path=str(raw.get("path") or raw.get("Path") or "/"),
        expires=expires_f if expires_f > 0 else -1,
        httpOnly=bool(raw.get("httpOnly", raw.get("httponly", False))),
        secure=bool(raw.get("secure", raw.get("Secure", False))),
        sameSite=_same_site(raw.get("sameSite", raw.get("samesite"))),
    )


def parse_netscape(text: str) -> list[Cookie]:
    """Parses the Netscape/Mozilla ``cookies.txt`` format (as exported by browser add-ons
    and ``curl``/``yt-dlp``)."""
    cookies: list[Cookie] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.rstrip("\r\n")
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_") :]
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"line {line_no}: expected 7 tab-separated fields")
        domain, include_subdomains, path, secure, expires, name, value = fields
        domain = domain.strip().lower()
        if include_subdomains.strip().upper() == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        try:
            expires_f = float(expires)
        except ValueError:
            expires_f = 0
        cookies.append(
            Cookie(
                name=name,
                value=value,
                domain=domain,
                path=path or "/",
                expires=expires_f if expires_f > 0 else -1,
                httpOnly=http_only,
                secure=secure.strip().upper() == "TRUE",
            )
        )
    return cookies


CookieFormat = Literal["auto", "json", "netscape", "storage_state"]


def parse_cookies(data: Any, fmt: CookieFormat = "auto") -> StorageState:
    """Parses cookies exported from a browser into a :class:`StorageState`.

    Accepts a JSON array of cookies (Cookie-Editor, EditThisCookie...), a Playwright
    storage state (``{"cookies": [...], "origins": [...]}``) as object or JSON string,
    or Netscape ``cookies.txt`` text.
    """
    try:
        if isinstance(data, str):
            stripped = data.strip()
            if not stripped:
                raise ValueError("empty cookie data")
            if fmt == "netscape" or (fmt == "auto" and stripped[0] not in "[{"):
                return StorageState(cookies=parse_netscape(stripped))
            data = json.loads(stripped)
        if isinstance(data, dict):
            raw_cookies = data.get("cookies")
            if not isinstance(raw_cookies, list):
                raise ValueError("object input must contain a 'cookies' list")
            cookies = [_from_json_cookie(c) for c in raw_cookies]
            origins = [OriginStorage.model_validate(o) for o in data.get("origins") or []]
            return StorageState(cookies=cookies, origins=origins)
        if isinstance(data, list):
            return StorageState(cookies=[_from_json_cookie(c) for c in data])
        raise ValueError("unsupported cookie data type")
    except ApiError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError(ErrorCode.SESSION_INVALID, f"Could not parse cookies: {exc}") from exc
