"""HTML -> clean main content (Markdown + plain text) and metadata.

The pipeline is deterministic: the same HTML always yields the same text, which is
what makes the content fingerprint stable.

1. :func:`clean_html` removes scripts, embeds and ad/cookie/share/related blocks
   (matched on class/id *tokens*, never removing the main article container).
2. :func:`main_text` / :func:`main_markdown` run trafilatura (boilerplate removal).
3. :func:`extract_metadata` reads OpenGraph / JSON-LD / meta tags, with trafilatura's
   metadata extraction as fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import trafilatura
from lxml import etree
from lxml import html as lxml_html

from ..core.dates import parse_datetime
from ..core.lang import normalize_language
from ..core.text import collapse_ws

logger = logging.getLogger(__name__)

_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "template",
    "canvas",
    "object",
    "embed",
    "link",
    "meta",
)
_NOISE_TOKENS = frozenset(
    {
        "ad",
        "ads",
        "adv",
        "advert",
        "adverts",
        "advertisement",
        "advertising",
        "adsbygoogle",
        "adslot",
        "adunit",
        "adsense",
        "dfp",
        "gpt",
        "sponsor",
        "sponsored",
        "promo",
        "promoted",
        "taboola",
        "outbrain",
        "teads",
        "newsletter",
        "cookie",
        "cookies",
        "consent",
        "gdpr",
        "popup",
        "modal",
        "social",
        "share",
        "sharing",
        "sharebar",
        "related",
        "recommended",
        "recirculation",
        "subscribe-box",
        "comments",
        "comment",
        "disqus",
    }
)
_TOKEN_SPLIT = re.compile(r"[\s_\-]+")
_PROTECTED_TAGS = frozenset({"html", "body", "main", "article"})
_MD_EXCESS_NEWLINES = re.compile(r"\n{3,}")


def _parse(html_text: str) -> lxml_html.HtmlElement | None:
    if not html_text or not html_text.strip():
        return None
    try:
        return lxml_html.document_fromstring(html_text)
    except (etree.ParserError, ValueError):
        try:
            return lxml_html.fromstring(html_text)
        except Exception:
            return None


def _is_noise(element: lxml_html.HtmlElement) -> bool:
    values = " ".join(filter(None, [element.get("class"), element.get("id")]))
    if not values:
        return False
    tokens = {t.lower() for t in _TOKEN_SPLIT.split(values) if t}
    if "subscribe" in tokens and "box" in tokens:
        tokens.add("subscribe-box")
    return bool(tokens & _NOISE_TOKENS)


def clean_html(html_text: str) -> str:
    """Removes non-content elements. Returns serialized HTML (or the input on failure)."""
    tree = _parse(html_text)
    if tree is None:
        return html_text or ""
    for element in tree.xpath("//" + " | //".join(_DROP_TAGS)):
        if element.getparent() is not None:
            element.drop_tree()
    for element in tree.xpath("//comment()"):
        if element.getparent() is not None:
            element.getparent().remove(element)
    for element in tree.xpath("//*[@hidden]"):
        if element.getparent() is not None and element.tag not in _PROTECTED_TAGS:
            element.drop_tree()

    body = tree.find("body")
    total = len(collapse_ws((body if body is not None else tree).text_content())) or 1
    candidates = [el for el in tree.iter() if isinstance(el.tag, str) and _is_noise(el)]
    for element in candidates:
        if element.getparent() is None or element.tag in _PROTECTED_TAGS:
            continue
        # Never drop a container holding a large part of the page's text.
        if len(collapse_ws(element.text_content())) > 0.3 * total:
            continue
        element.drop_tree()
    return etree.tostring(tree, encoding="unicode", method="html")


def _fallback_text(cleaned_html: str) -> str:
    tree = _parse(cleaned_html)
    if tree is None:
        return ""
    for element in tree.xpath("//nav|//header|//footer|//aside|//form|//button|//select"):
        if element.getparent() is not None:
            element.drop_tree()
    for block in tree.xpath("//p|//div|//li|//h1|//h2|//h3|//h4|//h5|//h6|//br|//tr|//section"):
        block.tail = ("\n" + block.tail) if block.tail else "\n"
    text = tree.text_content()
    lines = [collapse_ws(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def visible_text_length(html_text: str) -> int:
    """Cheap estimate of the amount of visible text (for thin-page heuristics)."""
    tree = _parse(html_text)
    if tree is None:
        return 0
    for element in tree.xpath("//script|//style|//noscript|//template|//svg"):
        if element.getparent() is not None:
            element.drop_tree()
    body = tree.find("body")
    return len(collapse_ws((body if body is not None else tree).text_content()))


_MD_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")


def clean_markdown_links(markdown: str) -> str:
    """Removes tracking parameters from the URLs of Markdown links and images."""
    from ..core.urls import canonicalize_url

    return _MD_LINK.sub(lambda m: f"]({canonicalize_url(m.group(1))})", markdown)


def extract_links(html_text: str, base_url: str, limit: int = 500) -> list[dict[str, str]]:
    from urllib.parse import urljoin

    from ..core.urls import canonicalize_url, is_http_url

    tree = _parse(html_text)
    if tree is None:
        return []
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for anchor in tree.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = canonicalize_url(urljoin(base_url, href))
        if not is_http_url(absolute) or absolute in seen:
            continue
        seen.add(absolute)
        links.append({"url": absolute, "text": collapse_ws(anchor.text_content())[:200]})
        if len(links) >= limit:
            break
    return links


def main_text(cleaned_html: str, url: str | None = None) -> str:
    """Plain text of the main content (used for the fingerprint)."""
    text: str | None = None
    if cleaned_html:
        try:
            text = trafilatura.extract(
                cleaned_html,
                url=url,
                output_format="txt",
                include_comments=False,
                include_tables=True,
                include_images=False,
                include_links=False,
                include_formatting=False,
                deduplicate=False,
            )
        except Exception:  # pragma: no cover - defensive, trafilatura edge cases
            logger.debug("trafilatura txt extraction failed", exc_info=True)
    if not text or len(text.strip()) < 40:
        fallback = _fallback_text(cleaned_html)
        if len(fallback) > len(text or ""):
            text = fallback
    return (text or "").strip()


def main_markdown(cleaned_html: str, url: str | None = None) -> str | None:
    try:
        markdown = trafilatura.extract(
            cleaned_html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_images=True,
            include_links=True,
            include_formatting=True,
            deduplicate=False,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("trafilatura markdown extraction failed", exc_info=True)
        markdown = None
    if markdown:
        markdown = _MD_EXCESS_NEWLINES.sub("\n\n", markdown).strip()
    return markdown or None


def full_markdown(cleaned_html: str) -> str:
    """Markdown of the whole (cleaned) page body, for structured extraction."""
    from markdownify import markdownify

    tree = _parse(cleaned_html)
    if tree is None:
        return ""
    body = tree.find("body")
    fragment = etree.tostring(body if body is not None else tree, encoding="unicode", method="html")
    markdown = markdownify(fragment, heading_style="ATX", bullets="-", strip=["img"])
    lines = [line.rstrip() for line in markdown.splitlines()]
    return _MD_EXCESS_NEWLINES.sub("\n\n", "\n".join(lines)).strip()


# ----------------------------------------------------------------------------- metadata
@dataclass
class PageMetadata:
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    language: str | None = None
    image: str | None = None
    site_name: str | None = None
    description: str | None = None
    canonical_url: str | None = None
    keywords: list[str] = field(default_factory=list)


def _meta(tree: lxml_html.HtmlElement, *names: str) -> str | None:
    for name in names:
        for attr in ("property", "name", "itemprop", "http-equiv"):
            values = tree.xpath(f"//meta[@{attr}=$n]/@content", n=name)
            for value in values:
                value = collapse_ws(value)
                if value:
                    return value
    return None


def _json_ld_objects(tree: lxml_html.HtmlElement) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for script in tree.xpath("//script[@type='application/ld+json']"):
        raw = (script.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "@graph" in item:
                    stack.append(item["@graph"])
                objects.append(item)
    return objects


def _ld_name(value: Any) -> str | None:
    if isinstance(value, str):
        return collapse_ws(value) or None
    if isinstance(value, dict):
        return _ld_name(value.get("name"))
    if isinstance(value, list):
        names = [n for n in (_ld_name(v) for v in value) if n]
        return ", ".join(dict.fromkeys(names)) if names else None
    return None


def _ld_image(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl")
    if isinstance(value, list) and value:
        return _ld_image(value[0])
    return None


_ARTICLE_TYPES = {
    "newsarticle",
    "article",
    "blogposting",
    "reportagenewsarticle",
    "webpage",
    "analysisnewsarticle",
    "opinionnewsarticle",
    "techarticle",
    "report",
}


def extract_metadata(
    html_text: str, url: str | None = None, headers: dict | None = None
) -> PageMetadata:
    """Metadata from OpenGraph, JSON-LD and meta tags (trafilatura as fallback).

    Uses the raw HTML: ``clean_html`` drops ``<script>`` (JSON-LD) and ``<meta>`` tags.
    """
    meta = PageMetadata()
    tree = _parse(html_text)
    if tree is None:
        return meta

    ld_article: dict[str, Any] = {}
    for obj in _json_ld_objects(tree):
        types = obj.get("@type")
        type_names = {
            t.lower() for t in (types if isinstance(types, list) else [types]) if isinstance(t, str)
        }
        if type_names & _ARTICLE_TYPES and not ld_article:
            ld_article = obj

    title_tag = tree.findtext(".//title")
    meta.title = (
        _meta(tree, "og:title", "twitter:title")
        or _ld_name(ld_article.get("headline"))
        or collapse_ws(title_tag)
        or None
    )
    meta.description = _meta(tree, "og:description", "description", "twitter:description")
    meta.image = _meta(
        tree, "og:image", "og:image:url", "twitter:image", "twitter:image:src"
    ) or _ld_image(ld_article.get("image"))
    meta.site_name = _meta(tree, "og:site_name", "application-name") or _ld_name(
        ld_article.get("publisher")
    )
    meta.author = _ld_name(ld_article.get("author")) or _meta(
        tree, "author", "article:author", "parsely-author", "sailthru.author", "dc.creator"
    )
    if meta.author and meta.author.startswith(("http://", "https://")):
        meta.author = None
    meta.published_at = parse_datetime(
        _meta(
            tree,
            "article:published_time",
            "og:published_time",
            "datePublished",
            "publishdate",
            "pubdate",
            "date",
            "dc.date",
            "dc.date.issued",
            "sailthru.date",
            "parsely-pub-date",
        )
        or ld_article.get("datePublished")
        or ld_article.get("dateCreated")
    )
    meta.modified_at = parse_datetime(
        _meta(tree, "article:modified_time", "og:updated_time", "dateModified")
        or ld_article.get("dateModified")
    )
    if meta.published_at is None:
        times = tree.xpath(
            "//article//time/@datetime | //time[@itemprop='datePublished']/@datetime"
        )
        if times:
            meta.published_at = parse_datetime(times[0])

    html_lang = tree.get("lang") or tree.get("xml:lang")
    meta.language = normalize_language(
        html_lang
        or _meta(tree, "og:locale", "content-language", "language", "dc.language")
        or (headers or {}).get("content-language")
    )
    canonical = tree.xpath("//link[@rel='canonical']/@href")
    if canonical:
        from urllib.parse import urljoin

        meta.canonical_url = (
            urljoin(url or "", canonical[0].strip()) if url else canonical[0].strip()
        )
    keywords = _meta(tree, "keywords", "news_keywords")
    if keywords:
        meta.keywords = [k.strip() for k in keywords.split(",") if k.strip()][:30]

    if not (meta.author and meta.published_at and meta.title and meta.site_name):
        try:
            doc = trafilatura.extract_metadata(html_text, default_url=url)
        except Exception:  # pragma: no cover - defensive
            doc = None
        if doc is not None:
            meta.title = meta.title or getattr(doc, "title", None)
            meta.author = meta.author or getattr(doc, "author", None)
            meta.site_name = meta.site_name or getattr(doc, "sitename", None)
            meta.description = meta.description or getattr(doc, "description", None)
            meta.image = meta.image or getattr(doc, "image", None)
            if meta.published_at is None:
                meta.published_at = parse_datetime(getattr(doc, "date", None))
    if meta.image and url:
        from urllib.parse import urljoin

        meta.image = urljoin(url, meta.image)
    return meta
