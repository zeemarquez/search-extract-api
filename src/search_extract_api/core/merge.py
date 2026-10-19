"""Merging and URL-level deduplication of results coming from several sources.

Results are grouped by :func:`~search_extract_api.core.urls.url_key` (and, optionally,
by normalized headline within the same site, which catches redirect URLs such as
Google News links). Fields are filled from the highest-priority source that has them,
the list of providers is unioned, and ranks are fused with Reciprocal Rank Fusion.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from ..schemas.common import Result
from .text import normalize_title
from .urls import is_weak_url, normalize_domain, result_id, site_of, url_key

R = TypeVar("R", bound=Result)

RRF_K = 60
_MIN_TITLE_KEY_LEN = 20
_SKIP_FIELDS = {"id", "url", "providers", "score", "extra"}


def _effective_site(result: Result) -> str:
    if is_weak_url(result.url):
        return normalize_domain(result.source or "")
    return site_of(result.url)


def _title_key(result: Result) -> str | None:
    title = normalize_title(result.title)
    site = _effective_site(result)
    if len(title) < _MIN_TITLE_KEY_LEN or not site:
        return None
    return f"{site}|{title}"


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _absorb(target: Result, other: Result) -> None:
    """Fills ``target``'s empty fields from ``other`` and unions providers/extra."""
    if is_weak_url(target.url) and not is_weak_url(other.url):
        alternates = target.extra.setdefault("alternate_urls", [])
        if target.url not in alternates:
            alternates.append(target.url)
        target.url = other.url
        target.id = result_id(other.url)
    elif other.url != target.url:
        alternates = target.extra.setdefault("alternate_urls", [])
        if other.url not in alternates:
            alternates.append(other.url)

    for name in type(target).model_fields:
        if name in _SKIP_FIELDS or name not in type(other).model_fields:
            continue
        if _is_empty(getattr(target, name)) and not _is_empty(getattr(other, name)):
            setattr(target, name, getattr(other, name))

    for provider in other.providers:
        if provider not in target.providers:
            target.providers.append(provider)
    for key, value in other.extra.items():
        if key == "alternate_urls":
            alternates = target.extra.setdefault("alternate_urls", [])
            for alt in value:
                if alt not in alternates and alt != target.url:
                    alternates.append(alt)
        else:
            target.extra.setdefault(key, value)


def merge_results(
    ranked_lists: Sequence[tuple[str, Sequence[R]]],
    *,
    title_merge: bool = True,
    rrf_k: int = RRF_K,
) -> list[R]:
    """Merges ranked result lists.

    ``ranked_lists`` is a sequence of ``(provider_name, results)`` in provider
    priority order; each ``results`` list is in that provider's rank order.
    Returns merged results sorted by fused score (highest first).
    """
    merged: list[R] = []
    scores: list[float] = []
    by_url: dict[str, int] = {}
    by_title: dict[str, int] = {}

    for provider, results in ranked_lists:
        for rank, original in enumerate(results, start=1):
            result = original.model_copy(deep=True)
            if provider and provider not in result.providers:
                result.providers.append(provider)
            ukey = url_key(result.url)
            tkey = _title_key(result) if title_merge else None

            index = by_url.get(ukey)
            if index is None and tkey is not None:
                index = by_title.get(tkey)

            contribution = 1.0 / (rrf_k + rank)
            if index is None:
                index = len(merged)
                merged.append(result)
                scores.append(contribution)
            else:
                _absorb(merged[index], result)
                scores[index] += contribution

            by_url.setdefault(ukey, index)
            by_url.setdefault(url_key(merged[index].url), index)
            if tkey is not None:
                by_title.setdefault(tkey, index)
            merged_tkey = _title_key(merged[index]) if title_merge else None
            if merged_tkey is not None:
                by_title.setdefault(merged_tkey, index)

    order = sorted(range(len(merged)), key=lambda i: (-scores[i], i))
    output: list[R] = []
    for i in order:
        item = merged[i]
        item.score = round(scores[i], 6)
        output.append(item)
    return output
