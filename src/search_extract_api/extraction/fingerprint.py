"""Content fingerprint: a hash of the *main content* that stays the same when only
ads, navigation, timestamps like "5 minutes ago" or tracking parameters change.

The fingerprint is always computed from the plain main text produced by
:func:`~search_extract_api.extraction.content.main_text` (a deterministic pipeline),
independent of which fetcher produced the HTML or which markdown generator ran.

* ``hash``    – ``sha256:<hex>`` of the normalized text: equal means unchanged.
* ``simhash`` – 64-bit SimHash of word 3-shingles: small Hamming distance means
  "almost the same" (useful to ignore trivial edits).
* ``version`` – bumped whenever normalization changes; only compare equal versions.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

import numpy as np
from pydantic import BaseModel, Field

FINGERPRINT_VERSION = 1

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_RELATIVE_TIME = re.compile(
    r"\b(?:"
    r"hace\s+(?:un|una|\d+)\s+(?:segundos?|minutos?|horas?|d[ií]as?)"
    r"|(?:a|an|one|\d+)\s+(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\s+ago"
    r"|il\s+y\s+a\s+\d+\s+(?:secondes?|minutes?|heures?|jours?)"
    r"|vor\s+\d+\s+(?:Sekunden|Minuten|Stunden|Tagen)"
    r"|(?:updated|actualizado|last\s+updated)\s*:?\s*(?:just\s+now|ahora\s+mismo)"
    r")\b",
    re.I,
)
_BOILERPLATE_LINE = re.compile(
    r"^\W*(?:advertisement|advertisment|advertising|publicidad|anuncio|sponsored(?:\s+content)?"
    r"|patrocinado|contenido\s+patrocinado|publicit[ée]|anzeige|pubblicit[àa]|ad|ads"
    r"|continue\s+reading|contin[úu]a\s+leyendo|sigue\s+leyendo|read\s+more|leer\s+m[áa]s"
    r"|m[áa]s\s+informaci[óo]n|share(?:\s+this)?|compartir|subscribe|suscr[íi]bete"
    r"|skip\s+(?:to\s+content|ad)|cerrar|close"
    r"|actualizado|updated|last\s+updated|publicado|published|[úu]ltima\s+actualizaci[óo]n)\W*$",
    re.I,
)
_WORD = re.compile(r"\w+", re.UNICODE)
_BIT_POSITIONS = np.arange(64, dtype=np.uint64)


class Fingerprint(BaseModel):
    hash: str = Field(description="sha256 of the normalized main text, as 'sha256:<hex>'.")
    simhash: str = Field(description="64-bit SimHash (16 hex chars) for near-duplicate checks.")
    version: int = FINGERPRINT_VERSION
    length: int = Field(description="Number of normalized tokens hashed.")


def normalize_for_fingerprint(text: str) -> list[str]:
    """Returns the normalized token sequence the fingerprint is computed from."""
    if not text:
        return []
    text = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = _URL.sub(" ", line)
        line = _RELATIVE_TIME.sub(" ", line)
        if not line.strip() or _BOILERPLATE_LINE.match(line):
            continue
        kept.append(line)
    return _WORD.findall(" ".join(kept).casefold())


def simhash64(tokens: list[str], shingle: int = 3) -> int:
    if not tokens:
        return 0
    if len(tokens) < shingle:
        features = [" ".join(tokens)]
    else:
        features = [" ".join(tokens[i : i + shingle]) for i in range(len(tokens) - shingle + 1)]
    hashes = np.fromiter(
        (
            int.from_bytes(hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest(), "big")
            for f in features
        ),
        dtype=np.uint64,
        count=len(features),
    )
    bits = (hashes[:, None] >> _BIT_POSITIONS) & np.uint64(1)
    counts = bits.sum(axis=0, dtype=np.int64)
    threshold = len(features) / 2
    value = 0
    for position, count in enumerate(counts.tolist()):
        if count > threshold:
            value |= 1 << position
    return value


def compute_fingerprint(main_text: str) -> Fingerprint:
    tokens = normalize_for_fingerprint(main_text)
    digest = hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()
    return Fingerprint(
        hash=f"sha256:{digest}",
        simhash=f"{simhash64(tokens):016x}",
        version=FINGERPRINT_VERSION,
        length=len(tokens),
    )


def simhash_similarity(a: str, b: str) -> float:
    """1.0 = identical, ~0.5 = unrelated."""
    distance = bin(int(a, 16) ^ int(b, 16)).count("1")
    return round(1 - distance / 64, 4)
