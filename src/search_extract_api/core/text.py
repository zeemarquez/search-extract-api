"""Small text helpers shared by adapters and extractors."""

from __future__ import annotations

import html
import re
import unicodedata

from lxml import html as lxml_html

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
_TITLE_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{2,40}$")


def collapse_ws(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", _ZERO_WIDTH.sub("", text)).strip()


def strip_html(text: str | None) -> str:
    """Converts an HTML fragment to plain text (best effort, never raises)."""
    if not text:
        return ""
    if "<" not in text:
        return collapse_ws(html.unescape(text))
    try:
        fragment = lxml_html.fragment_fromstring(text, create_parent="div")
        for bad in fragment.xpath(".//script|.//style"):
            bad.drop_tree()
        return collapse_ws(fragment.text_content())
    except Exception:
        return collapse_ws(html.unescape(_TAG.sub(" ", text)))


def truncate(text: str | None, max_chars: int) -> str | None:
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(title: str | None, *, drop_suffix: bool = True) -> str:
    """Normalized form of a headline used for matching across sources."""
    if not title:
        return ""
    value = collapse_ws(html.unescape(title))
    if drop_suffix:
        value = _TITLE_SUFFIX.sub("", value)
    value = strip_accents(value).casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    return _WS.sub(" ", value).strip()


def split_title_outlet(title: str) -> tuple[str, str | None]:
    """Splits Google-News-style ``"Headline - Outlet"`` titles."""
    title = collapse_ws(title)
    for sep in (" - ", " | ", " – ", " — "):
        if sep in title:
            head, _, tail = title.rpartition(sep)
            if head and 1 < len(tail) <= 60:
                return head.strip(), tail.strip()
    return title, None
