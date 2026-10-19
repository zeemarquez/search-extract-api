"""Language normalization and lightweight detection."""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# English names (as used by GDELT and others) -> ISO 639-1.
LANGUAGE_NAMES: dict[str, str] = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "catalan": "ca",
    "dutch": "nl",
    "russian": "ru",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "arabic": "ar",
    "polish": "pl",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "greek": "el",
    "turkish": "tr",
    "hebrew": "he",
    "hindi": "hi",
    "ukrainian": "uk",
    "romanian": "ro",
    "czech": "cs",
    "hungarian": "hu",
    "galician": "gl",
    "basque": "eu",
    "indonesian": "id",
    "vietnamese": "vi",
    "thai": "th",
    "bulgarian": "bg",
    "croatian": "hr",
    "serbian": "sr",
    "slovak": "sk",
    "slovenian": "sl",
    "lithuanian": "lt",
    "latvian": "lv",
    "estonian": "et",
}
ISO_TO_NAME = {code: name for name, code in LANGUAGE_NAMES.items()}


def normalize_language(value: str | None) -> str | None:
    """``es-ES`` / ``es_ES`` / ``Spanish`` / ``spa`` -> ``es``."""
    if not value:
        return None
    text = value.strip().lower().replace("_", "-")
    if not text or text in {"all", "auto", "und", "*"}:
        return None
    if text in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[text]
    primary = text.split("-", 1)[0]
    if len(primary) == 2 and primary.isalpha():
        return primary
    three_letter = {
        "eng": "en",
        "spa": "es",
        "fra": "fr",
        "fre": "fr",
        "deu": "de",
        "ger": "de",
        "ita": "it",
        "por": "pt",
        "cat": "ca",
        "nld": "nl",
        "dut": "nl",
        "rus": "ru",
        "zho": "zh",
        "chi": "zh",
        "jpn": "ja",
        "kor": "ko",
        "ara": "ar",
        "pol": "pl",
    }
    return three_letter.get(primary)


@lru_cache(maxsize=1)
def _identifier():  # pragma: no cover - trivial loader
    try:
        from py3langid import langid

        if hasattr(langid.LanguageIdentifier, "from_modelpath"):  # py3langid >= 0.3
            return langid.LanguageIdentifier.from_modelpath(
                langid.MODEL_DIR / langid.MODEL_FILE, norm_probs=True
            )
        return langid.LanguageIdentifier.from_pickled_model(  # pragma: no cover - old API
            langid.MODEL_FILE, norm_probs=True
        )
    except Exception as exc:
        logger.warning("Language identification unavailable: %s", exc)
        return None


def detect_language(text: str | None, *, min_chars: int = 40, min_prob: float = 0.80) -> str | None:
    """Detects the language of ``text``; returns ``None`` when unsure."""
    if not text or len(text.strip()) < min_chars:
        return None
    identifier = _identifier()
    if identifier is None:
        return None
    try:
        lang, prob = identifier.classify(text[:4000])
    except Exception:
        return None
    return lang if prob >= min_prob else None
