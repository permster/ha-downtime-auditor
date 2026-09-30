"""Pure schedule math (no Home Assistant imports, so it is unit-testable).

All functions take timezone-aware datetimes. "Local" wall-clock logic is done
in the supplied tzinfo, the same way Home Assistant's time triggers work.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date, datetime, time, timedelta, tzinfo

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def iter_local_dates(start: datetime, end: datetime, tz: tzinfo) -> Iterator[date]:
    """Yield every local calendar date touched by [start, end]."""
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    while day <= last:
        yield day
        day += timedelta(days=1)


def local_dt(day: date, at: time, tz: tzinfo) -> datetime:
    """Combine a local date + wall time into an aware datetime."""
    return datetime.combine(day, at.replace(tzinfo=None), tzinfo=tz)


def normalize_weekdays(weekday: str | Iterable[str] | None) -> set[int] | None:
    """Turn HA's weekday option ('mon' or ['mon','tue']) into weekday ints."""
    if weekday is None:
        return None
    if isinstance(weekday, str):
        weekday = [weekday]
    out = {WEEKDAYS.index(str(w).lower()[:3]) for w in weekday}
    return out or None


def daily_occurrences(
    at: time,
    start: datetime,
    end: datetime,
    tz: tzinfo,
    weekdays: set[int] | None = None,
    offset: timedelta = timedelta(0),
) -> list[datetime]:
    """Occurrences of a daily wall-clock time (+offset) strictly inside (start, end]."""
    out: list[datetime] = []
    # Pad a day each side so offsets that push across midnight are included.
    for day in iter_local_dates(start - timedelta(days=1), end + timedelta(days=1), tz):
        occ = local_dt(day, at, tz)
        if weekdays is not None and occ.weekday() not in weekdays:
            continue
        occ = occ + offset
        if start < occ <= end:
            out.append(occ)
    return sorted(out)


def _parse_pattern(value, maximum: int) -> set[int] | None:
    """Parse a time_pattern field. None => wildcard handled by caller."""
    if value is None:
        return None
    value = str(value).strip()
    if value == "*":
        return set(range(maximum + 1))
    if value.startswith("/"):
        step = int(value[1:])
        if step <= 0:
            return set()
        return {v for v in range(maximum + 1) if v % step == 0}
    return {int(value)}


def time_pattern_sets(hours=None, minutes=None, seconds=None):
    """Replicate HA's defaulting rules for time_pattern.

    If a larger unit is given, smaller unspecified units default to 0.
    Unspecified larger units are wildcards.
    """
    if minutes is None and hours is not None:
        minutes = 0
    if seconds is None and minutes is not None:
        seconds = 0
    h = _parse_pattern(hours, 23)
    m = _parse_pattern(minutes, 59)
    s = _parse_pattern(seconds, 59)
    return (
        h if h is not None else set(range(24)),
        m if m is not None else set(range(60)),
        s if s is not None else set(range(60)),
    )


def time_pattern_occurrences(
    start: datetime,
    end: datetime,
    tz: tzinfo,
    hours=None,
    minutes=None,
    seconds=None,
    limit: int = 20000,
) -> tuple[int, list[datetime]]:
    """Count matches in (start, end]; return (count, first `limit` matches)."""
    hs, ms, ss = time_pattern_sets(hours, minutes, seconds)
    count = 0
    matches: list[datetime] = []
    for day in iter_local_dates(start, end, tz):
        for hour in sorted(hs):
            for minute in sorted(ms):
                base = local_dt(day, time(hour, minute), tz)
                if base + timedelta(seconds=59) <= start or base > end:
                    continue
                for sec in sorted(ss):
                    occ = base + timedelta(seconds=sec)
                    if start < occ <= end:
                        count += 1
                        if len(matches) < limit:
                            matches.append(occ)
    return count, matches


def state_matches(value, wanted) -> bool:
    """HA-ish match: None/[] => any; list => membership; scalar => equality."""
    if wanted is None:
        return True
    if isinstance(wanted, (list, tuple, set)):
        if not wanted:
            return True
        return value in wanted
    return value == wanted


def in_numeric_range(value, above, below) -> bool | None:
    """Return whether value satisfies the numeric_state range, None if not numeric."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if above is not None and not num > float(above):
        return False
    if below is not None and not num < float(below):
        return False
    return True


def fmt_duration(seconds: float) -> str:
    """Human-friendly duration."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"
