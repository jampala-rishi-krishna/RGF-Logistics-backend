from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

_HHMMSS_RE = re.compile(r"(\d{2}):(\d{2}):\d{2}")


def minutes_since_midnight(value) -> int | None:
    """Ported from optimization-data.js/routes-module's minutesSinceMidnight: regex-extracts
    HH:MM from anywhere in a string (datetime or bare time-of-day), ignores seconds."""
    if value is None:
        return None
    match = _HHMMSS_RE.search(str(value))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def eta_from_arrival_min(arrival_min: float) -> datetime:
    """arrival_min from OR-Tools' Time dimension is minutes-since-midnight (0-1440) on the
    planning day - convert to a real calendar timestamp, anchored to today's UTC midnight."""
    today_midnight = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
    return today_midnight + timedelta(minutes=arrival_min)
