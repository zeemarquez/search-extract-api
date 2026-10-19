"""Date parsing helpers. All datetimes produced here are timezone-aware (UTC)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

TIME_RANGES: dict[str, timedelta] = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=31),
    "year": timedelta(days=366),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: Any) -> datetime | None:
    """Parses ISO 8601, RFC 2822, epoch seconds/ms, ``struct_time`` and compact forms."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, time.struct_time):
        try:
            return datetime(*value[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds > 1e12:  # milliseconds
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and len(text) in (10, 13):
        return parse_datetime(int(text))
    # GDELT style: 20241019T120000Z
    if len(text) == 16 and text[8] == "T" and text.endswith("Z") and text[:8].isdigit():
        try:
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            pass
    if len(text) == 14 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            pass
    iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return ensure_utc(datetime.fromisoformat(iso))
    except ValueError:
        pass
    try:
        return ensure_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def resolve_window(
    time_range: str | None, since: datetime | None, until: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Combines ``time_range`` with explicit ``since``/``until`` bounds."""
    since = ensure_utc(since)
    until = ensure_utc(until)
    if time_range and not since:
        since = utcnow() - TIME_RANGES[time_range]
    return since, until


def time_range_for(since: datetime | None, now: datetime | None = None) -> str | None:
    """Smallest named time range covering ``since`` (for engines that only take ranges)."""
    if since is None:
        return None
    delta = (now or utcnow()) - ensure_utc(since)
    for name in ("day", "week", "month", "year"):
        if delta <= TIME_RANGES[name]:
            return name
    return None
