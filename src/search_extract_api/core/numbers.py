"""Parsing of prices, areas and counts written in European or US formats."""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\d[\d.,\s  ]*")
_CURRENCIES = {"€": "EUR", "eur": "EUR", "$": "USD", "usd": "USD", "£": "GBP", "gbp": "GBP"}


def parse_number(text: str | None) -> float | None:
    """``"350.000 €"`` -> 350000, ``"1,250.50"`` -> 1250.5, ``"90,5 m²"`` -> 90.5."""
    if not text:
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    raw = re.sub(r"[\s  ]", "", match.group(0)).rstrip(".,")
    if not raw:
        return None
    if "," in raw and "." in raw:
        # The last separator is the decimal one.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        head, _, tail = raw.rpartition(",")
        raw = raw.replace(",", "") if len(tail) == 3 and head else raw.replace(",", ".")
    elif raw.count(".") >= 1:
        head, _, tail = raw.rpartition(".")
        if len(tail) == 3 and head:
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_int(text: str | None) -> int | None:
    value = parse_number(text)
    return int(value) if value is not None else None


def parse_currency(text: str | None, default: str | None = None) -> str | None:
    if not text:
        return default
    lowered = text.lower()
    for symbol, code in _CURRENCIES.items():
        if symbol in lowered:
            return code
    return default
