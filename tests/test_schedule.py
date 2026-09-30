from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.downtime_auditor import schedule as s

TZ = ZoneInfo("America/Phoenix")


def dt(*a):
    return datetime(*a, tzinfo=TZ)


def test_daily_inside_window():
    occ = s.daily_occurrences(time(6, 30), dt(2026, 9, 29, 6, 0), dt(2026, 9, 29, 7, 0), TZ)
    assert occ == [dt(2026, 9, 29, 6, 30)]


def test_daily_across_days_and_weekday_filter():
    # Fri 2026-10-02 .. Mon 2026-10-05
    occ = s.daily_occurrences(time(8, 0), dt(2026, 10, 2, 0, 0), dt(2026, 10, 5, 23, 0), TZ)
    assert len(occ) == 4
    wk = s.normalize_weekdays(["sat", "sun"])
    occ = s.daily_occurrences(time(8, 0), dt(2026, 10, 2, 0, 0), dt(2026, 10, 5, 23, 0), TZ, wk)
    assert [o.day for o in occ] == [3, 4]


def test_boundaries_exclusive_start_inclusive_end():
    assert s.daily_occurrences(time(6, 0), dt(2026, 9, 29, 6, 0), dt(2026, 9, 29, 7, 0), TZ) == []
    assert s.daily_occurrences(time(7, 0), dt(2026, 9, 29, 6, 0), dt(2026, 9, 29, 7, 0), TZ) == [dt(2026, 9, 29, 7, 0)]


def test_offset_crosses_midnight():
    occ = s.daily_occurrences(
        time(23, 50), dt(2026, 9, 30, 0, 0), dt(2026, 9, 30, 1, 0), TZ, offset=timedelta(minutes=20)
    )
    assert occ == [dt(2026, 9, 30, 0, 10)]


def test_time_pattern_defaults_and_steps():
    # minutes="/15" => seconds default 0, hours wildcard
    count, occ = s.time_pattern_occurrences(dt(2026, 9, 29, 1, 0), dt(2026, 9, 29, 2, 0), TZ, minutes="/15")
    assert count == 4
    assert occ[0] == dt(2026, 9, 29, 1, 15) and occ[-1] == dt(2026, 9, 29, 2, 0)
    # hours=3 => minute 0 second 0
    count, _ = s.time_pattern_occurrences(dt(2026, 9, 29, 0, 0), dt(2026, 9, 30, 12, 0), TZ, hours=3)
    assert count == 2
    # every second for 10 seconds
    count, _ = s.time_pattern_occurrences(dt(2026, 9, 29, 1, 0, 0), dt(2026, 9, 29, 1, 0, 10), TZ, seconds="*")
    assert count == 10


def test_numeric_range():
    assert s.in_numeric_range("31", 30, None) is True
    assert s.in_numeric_range("30", 30, None) is False
    assert s.in_numeric_range("x", 30, None) is None
    assert s.in_numeric_range(5, None, 10) is True


def test_state_matches():
    assert s.state_matches("on", None)
    assert s.state_matches("on", ["on", "open"])
    assert not s.state_matches("off", "on")
