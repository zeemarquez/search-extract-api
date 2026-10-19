"""A1 — URL canonicalization and merging/deduplication of results across sources."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from search_extract_api.core.merge import merge_results
from search_extract_api.core.urls import (
    canonicalize_url,
    domain_matches,
    normalize_domain,
    result_id,
    url_key,
)
from search_extract_api.schemas.common import Result
from search_extract_api.verticals.news.schemas import NewsResult

pytestmark = pytest.mark.a1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "HTTPS://Example.COM:443/a/../b/?utm_source=x&b=2&a=1#section",
            "https://example.com/b/?a=1&b=2",
        ),
        ("http://example.com", "http://example.com/"),
        ("https://example.com/p?fbclid=abc&gclid=1&id=7", "https://example.com/p?id=7"),
        ("https://example.com/app#!/route", "https://example.com/app#!/route"),
        (
            "https://www.google.com/url?q=https://elpais.com/x%3Futm_medium%3Dsocial&sa=U",
            "https://elpais.com/x",
        ),
        (
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage",
            "https://example.org/page",
        ),
        ("https://user:pw@example.com:8080/x", "https://example.com:8080/x"),
        ("https://example.com/caf%c3%a9?x=%7e", "https://example.com/caf%C3%A9?x=~"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_canonicalize_is_idempotent() -> None:
    url = "https://Example.com/a/b/?z=1&utm_campaign=x&a=2"
    once = canonicalize_url(url)
    assert canonicalize_url(once) == once


@pytest.mark.parametrize(
    "variant",
    [
        "https://www.example.com/news/story",
        "http://example.com/news/story/",
        "https://m.example.com/news/story?utm_source=rss",
        "https://example.com/news/story/amp",
        "https://amp.example.com/news/story",
        "https://example.com/news/story?outputType=amp",
    ],
)
def test_url_key_matches_variants(variant: str) -> None:
    assert url_key(variant) == url_key("https://example.com/news/story")
    assert result_id(variant) == result_id("https://example.com/news/story")


def test_url_key_keeps_distinct_documents_apart() -> None:
    assert url_key("https://example.com/a?id=1") != url_key("https://example.com/a?id=2")
    assert url_key("https://example.com/A") != url_key("https://example.com/a")
    assert url_key("https://a.example.com/x") != url_key("https://b.example.com/x")


def test_domain_helpers() -> None:
    assert normalize_domain("https://www.LinkedIn.com/feed") == "linkedin.com"
    assert domain_matches("es.linkedin.com", "linkedin.com")
    assert domain_matches("linkedin.com", "linkedin.com")
    assert not domain_matches("evil-linkedin.com", "linkedin.com")


def test_result_schema_canonicalizes_and_sets_id() -> None:
    result = Result(url="https://Example.com/x?utm_source=a", title="t", providers=["p"])
    assert result.url == "https://example.com/x"
    assert result.id == result_id("https://example.com/x")


def test_merge_dedups_url_variants_and_unions_providers() -> None:
    searxng = [
        Result(
            url="https://www.example.com/story?utm_source=x", title="Story", providers=["searxng"]
        ),
        Result(url="https://other.org/a", title="Other", providers=["searxng"]),
    ]
    tavily = [
        Result(
            url="https://example.com/story/",
            title="Story (tavily)",
            snippet="from tavily",
            published_at=datetime(2026, 10, 1, tzinfo=UTC),
            providers=["tavily"],
        ),
    ]
    merged = merge_results([("searxng", searxng), ("tavily", tavily)])
    assert len(merged) == 2
    story = merged[0]
    assert story.providers == ["searxng", "tavily"]
    # Higher-priority provider keeps its fields; empty ones are filled from others.
    assert story.title == "Story"
    assert story.snippet == "from tavily"
    assert story.published_at == datetime(2026, 10, 1, tzinfo=UTC)
    # Found by two sources -> ranked first by reciprocal rank fusion.
    assert story.score > merged[1].score


def test_merge_fuses_ranks() -> None:
    a = [Result(url=f"https://s.com/{i}", providers=["a"]) for i in range(5)]
    b = [Result(url=f"https://s.com/{i}", providers=["b"]) for i in (4, 3)]
    merged = merge_results([("a", a), ("b", b)])
    assert [r.url for r in merged[:2]] == ["https://s.com/3", "https://s.com/4"] or [
        r.url for r in merged[:2]
    ] == ["https://s.com/4", "https://s.com/3"]
    assert len(merged) == 5


def test_merge_by_headline_resolves_google_news_redirects() -> None:
    gnews = [
        NewsResult(
            url="https://news.google.com/rss/articles/CBMiXGh0dHBzOi8vZWxwYWlzLmNvbS94",
            title="El Gobierno aprueba el nuevo plan de vivienda",
            outlet="El País",
            source="elpais.com",
            providers=["google_news"],
        )
    ]
    searxng = [
        NewsResult(
            url="https://elpais.com/economia/2026-10-18/plan-vivienda.html",
            title="El Gobierno aprueba el nuevo plan de vivienda | El País",
            source="elpais.com",
            providers=["searxng_news"],
        )
    ]
    merged = merge_results([("searxng_news", searxng), ("google_news", gnews)])
    assert len(merged) == 1
    item = merged[0]
    assert item.url.startswith("https://elpais.com/")
    assert item.outlet == "El País"
    assert set(item.providers) == {"searxng_news", "google_news"}
    assert any("news.google.com" in u for u in item.extra.get("alternate_urls", []))


def test_merge_never_merges_same_title_from_different_sites() -> None:
    title = "Breaking: major earthquake hits the region tonight"
    merged = merge_results(
        [
            ("a", [Result(url="https://site-a.com/1", title=title, source="site-a.com")]),
            ("b", [Result(url="https://site-b.com/9", title=title, source="site-b.com")]),
        ]
    )
    assert len(merged) == 2


def test_merge_does_not_mutate_inputs() -> None:
    original = Result(url="https://x.com/1", providers=["a"])
    merge_results([("a", [original]), ("b", [Result(url="https://x.com/1", providers=["b"])])])
    assert original.providers == ["a"]
