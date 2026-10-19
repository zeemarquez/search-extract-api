"""Grouping of results that describe the same thing.

Two similarity backends:

* **embeddings** – cosine similarity of embedding vectors (any OpenAI-compatible
  embeddings endpoint).
* **lexical** – TF-IDF cosine over words, used when no embeddings model is available.

Items with the same canonical URL are always grouped. Clustering is greedy and
deterministic: each item joins the most similar existing cluster (by centroid) if the
similarity reaches the threshold, otherwise it starts a new cluster. The first item of
a cluster (input order) is its representative.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

import numpy as np

from ..core.text import strip_accents
from ..core.urls import url_key

DEFAULT_THRESHOLDS = {"embeddings": 0.86, "lexical": 0.5}

_STOPWORDS = frozenset(
    """
    the a an and or of to in on for with at by from as is are was were be been it its this that
    these those has have had not but they their them he she his her you your we our will would
    can could should about after before over under into than then there here what which who whom
    how when where why also more most new says said
    el la los las un una unos unas y o de del al en con por para como es son fue fueron ser ha
    han hay que se su sus lo le les ya mas más pero sin sobre entre tras este esta estos estas
    ese esa eso muy también tambien según segun dice dijo desde hasta cuando donde porque
    le la les des du et ou en dans pour par sur avec est sont une un ce cette qui que
    der die das und oder ein eine ist sind mit von zu im auf für
    """.split()
)
_WORD = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    words = _WORD.findall(strip_accents(text or "").casefold())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS and not w.isdigit()]


def lexical_vectors(texts: Sequence[str]) -> np.ndarray:
    """L2-normalized TF-IDF matrix (rows = texts)."""
    docs = [Counter(tokenize(t)) for t in texts]
    vocab: dict[str, int] = {}
    df: Counter[str] = Counter()
    for doc in docs:
        for term in doc:
            df[term] += 1
            vocab.setdefault(term, len(vocab))
    n = len(docs)
    matrix = np.zeros((n, max(1, len(vocab))), dtype=np.float32)
    for row, doc in enumerate(docs):
        for term, count in doc.items():
            idf = math.log((1 + n) / (1 + df[term])) + 1.0
            matrix[row, vocab[term]] = (1.0 + math.log(count)) * idf
    return normalize_rows(matrix)


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def greedy_clusters(
    vectors: np.ndarray, threshold: float, groups: Sequence[Sequence[int]] | None = None
) -> list[list[int]]:
    """Clusters rows of ``vectors`` (assumed L2-normalized).

    ``groups`` optionally pre-groups indices (e.g. identical URLs); each group is
    clustered as a unit using its first member's vector.
    """
    units = [list(g) for g in groups] if groups else [[i] for i in range(len(vectors))]
    clusters: list[list[int]] = []
    centroids: list[np.ndarray] = []
    for unit in units:
        vector = vectors[unit[0]]
        best, best_sim = -1, -1.0
        if centroids:
            sims = np.stack(centroids) @ vector
            best = int(np.argmax(sims))
            best_sim = float(sims[best])
        if best >= 0 and best_sim >= threshold:
            clusters[best].extend(unit)
            members = vectors[clusters[best]]
            centroid = members.mean(axis=0)
            norm = np.linalg.norm(centroid)
            centroids[best] = centroid / norm if norm else centroid
        else:
            clusters.append(list(unit))
            centroids.append(vector)
    return clusters


def url_groups(urls: Sequence[str | None]) -> list[list[int]]:
    """Groups indices whose URLs are equivalent; items without URL stay alone."""
    by_key: dict[str, list[int]] = {}
    groups: list[list[int]] = []
    for index, url in enumerate(urls):
        if url:
            key = url_key(url)
            if key in by_key:
                by_key[key].append(index)
                continue
            by_key[key] = [index]
            groups.append(by_key[key])
        else:
            groups.append([index])
    return groups
