"""A1 — content fingerprint stability."""

from __future__ import annotations

import pytest

from search_extract_api.extraction.content import clean_html, main_text
from search_extract_api.extraction.fingerprint import (
    FINGERPRINT_VERSION,
    compute_fingerprint,
    normalize_for_fingerprint,
    simhash_similarity,
)

from .conftest import fixture_text

pytestmark = pytest.mark.a1

URL = "https://www.diarioejemplo.es/economia/plan-vivienda"


def fingerprint_of(name: str):
    return compute_fingerprint(main_text(clean_html(fixture_text(name)), URL))


def test_same_content_same_hash_despite_ads_and_page_chrome() -> None:
    # v2 changes ads (inline and banners), navigation, cookie banner, newsletter box,
    # share buttons, "related"/"most read" blocks, scripts, tracking parameters in links
    # and the "updated N minutes ago" timestamp — but not the article itself.
    v1 = fingerprint_of("article_v1.html")
    v2 = fingerprint_of("article_v2_ads_changed.html")
    assert v1.hash == v2.hash
    assert v1.simhash == v2.simhash
    assert v1.version == FINGERPRINT_VERSION
    assert v1.length > 50


def test_changed_content_changes_hash() -> None:
    v1 = fingerprint_of("article_v1.html")
    v3 = fingerprint_of("article_v3_content_changed.html")  # 3.500 -> 4.200 millones
    assert v1.hash != v3.hash
    # Still recognizably the same document.
    assert simhash_similarity(v1.simhash, v3.simhash) >= 0.75


def test_unrelated_content_is_dissimilar() -> None:
    a = compute_fingerprint(main_text(clean_html(fixture_text("article_v1.html")), URL))
    b = compute_fingerprint(
        "El equipo local ganó el partido por tres goles a cero en un encuentro marcado por la "
        "lluvia. El entrenador destacó la actitud de los jugadores y el apoyo de la afición, "
        "que llenó el estadio pese al mal tiempo durante toda la tarde del domingo."
    )
    assert a.hash != b.hash
    assert simhash_similarity(a.simhash, b.simhash) < 0.8


def test_fingerprint_is_deterministic() -> None:
    first = fingerprint_of("article_v1.html")
    for _ in range(3):
        assert fingerprint_of("article_v1.html") == first


def test_normalization_ignores_volatile_bits() -> None:
    base = "La reunión terminó sin acuerdo.\nLos sindicatos convocan huelga el lunes."
    noisy = (
        "Actualizado hace 3 minutos\n"
        "La reunión terminó   sin acuerdo!\n"
        "Publicidad\n"
        "Los sindicatos convocan huelga el lunes. https://t.co/abc?utm_source=x\n"
        "Compartir\n"
    )
    assert normalize_for_fingerprint(base) == normalize_for_fingerprint(noisy)
    assert compute_fingerprint(base).hash == compute_fingerprint(noisy).hash


def test_numbers_are_meaningful() -> None:
    assert (
        compute_fingerprint("Precio: 100 euros").hash
        != compute_fingerprint("Precio: 120 euros").hash
    )


def test_clean_html_keeps_article_even_if_container_has_noise_tokens() -> None:
    html = (
        "<html><body><div class='post has-share-buttons'>"
        + "".join(
            f"<p>Parrafo numero {i} con contenido real del articulo principal.</p>"
            for i in range(30)
        )
        + "</div><div class='ad'>Compra ahora</div></body></html>"
    )
    cleaned = clean_html(html)
    assert "Parrafo numero 29" in cleaned
    assert "Compra ahora" not in cleaned
