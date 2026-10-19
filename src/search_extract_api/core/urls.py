"""URL canonicalization, dedup keys and domain helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

# Query parameters that only carry tracking information.
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "ref_src",
        "ref_url",
        "xtor",
        "_ga",
        "_gl",
        "oly_enc_id",
        "oly_anon_id",
        "vero_id",
        "rb_clickid",
        "s_kwcid",
        "ef_id",
        "wt_mc",
        "wt.mc_id",
        "ocid",
        "cmpid",
        "sr_share",
        "spm",
        "ito",
        "fb_action_ids",
        "fb_action_types",
        "fb_source",
        "fb_ref",
        "trk",
        "trkcampaign",
        "smid",
        "smtyp",
        "share",
        "at_medium",
        "at_campaign",
        "at_custom1",
        "at_custom2",
        "guccounter",
        "guce_referrer",
        "guce_referrer_sig",
        "__twitter_impression",
    }
)
TRACKING_PREFIXES = ("utm_", "ns_", "pk_", "mtm_", "hsa_")

# Hosts whose URLs are redirectors / aggregators rather than the real document.
WEAK_HOSTS = frozenset({"news.google.com"})

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_KEY_HOST_PREFIXES = ("www.", "m.", "amp.", "mobile.")


def is_http_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)


def _unwrap_redirect(url: str) -> str:
    """Unwraps well-known redirect wrappers (Google, DuckDuckGo, Facebook, Bing)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    candidate: str | None = None
    if host.endswith("google.com") and parts.path == "/url":
        candidate = params.get("q") or params.get("url")
    elif host.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        candidate = params.get("uddg")
    elif host in {"l.facebook.com", "lm.facebook.com"} and parts.path == "/l.php":
        candidate = params.get("u")
    elif host.endswith("bing.com") and parts.path.startswith("/ck/a"):
        encoded = params.get("u", "")
        if encoded.startswith("a1"):
            encoded = encoded[2:]
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                candidate = base64.urlsafe_b64decode(padded).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                candidate = None
    if candidate and is_http_url(candidate):
        return candidate
    return url


def _normalize_percent(segment: str) -> str:
    def repl(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        if char in _UNRESERVED_CHARS:
            return char
        return "%" + match.group(1).upper()

    return _UNRESERVED.sub(repl, segment)


def _encode_host(host: str) -> str:
    host = host.strip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Returns a normalized, tracking-free version of ``url``.

    Lowercases scheme and host, removes default ports, credentials, fragments (except
    ``#!`` routes) and tracking parameters, sorts the query string and resolves dot
    segments. Invalid input is returned stripped but otherwise untouched.
    """
    if not url:
        return url
    url = _unwrap_redirect(url.strip())
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        return url
    host = _encode_host(parts.hostname)
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"

    path = _normalize_percent(parts.path or "/")
    if path != "/":
        trailing = path.endswith("/")
        path = posixpath.normpath(path)
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
        if trailing and not path.endswith("/"):
            path += "/"
    if not path.startswith("/"):
        path = "/" + path

    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query_items.sort(key=lambda kv: kv[0])
    query = urlencode(query_items, quote_via=quote, safe="/:@!$'()*,;")

    fragment = parts.fragment if parts.fragment.startswith("!") else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def url_key(url: str) -> str:
    """Aggressive key used to detect that two URLs point to the same document.

    Ignores scheme, ``www.``/``m.``/``amp.`` host prefixes, trailing slashes, AMP
    variants and ``index.html``-style suffixes.
    """
    canonical = canonicalize_url(url)
    try:
        parts = urlsplit(canonical)
    except ValueError:
        return canonical.lower()
    host = (parts.hostname or "").lower()
    for prefix in _KEY_HOST_PREFIXES:
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix) :]
            break
    path = unquote(parts.path or "/")
    for suffix in ("/amp", "/amp/", "/index.html", "/index.htm", "/index.php"):
        if path.endswith(suffix):
            path = path[: -len(suffix)] or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    if path.endswith(".amp"):
        path = path[:-4]
    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in {"amp", "outputtype", "output"}
    ]
    query = urlencode(sorted(query_items))
    key = f"{host}{path}"
    if query:
        key += f"?{query}"
    return key


def result_id(url: str) -> str:
    """Stable identifier for a document URL (same for equivalent URL variants)."""
    return hashlib.sha1(url_key(url).encode("utf-8")).hexdigest()[:16]


def host_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def site_of(url: str | None) -> str:
    """Host without the ``www.`` prefix, e.g. ``elpais.com``."""
    host = host_of(url)
    return host[4:] if host.startswith("www.") else host


def normalize_domain(value: str) -> str:
    """Normalizes user input like ``https://www.LinkedIn.com/feed`` to ``linkedin.com``."""
    value = (value or "").strip().lower()
    if "://" in value:
        value = host_of(value)
    value = value.split("/", 1)[0].split(":", 1)[0].strip(".")
    if value.startswith("www."):
        value = value[4:]
    return value


def domain_matches(host: str, domain: str) -> bool:
    """True if ``host`` is ``domain`` or one of its subdomains."""
    host = host.lower().strip(".")
    domain = domain.lower().strip(".")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def is_weak_url(url: str) -> bool:
    return host_of(url) in WEAK_HOSTS
