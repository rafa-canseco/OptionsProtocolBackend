from datetime import datetime, timezone, timedelta

from src.pricing.utils import get_friday_expiries, FRIDAY_WEEKDAY


def test_returns_exactly_three():
    result = get_friday_expiries()
    assert len(result) == 3


def test_all_are_fridays():
    for ts in get_friday_expiries():
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.weekday() == FRIDAY_WEEKDAY, f"{dt} is not a Friday"


def test_all_satisfy_contract_constraint():
    for ts in get_friday_expiries():
        assert ts % 86400 == 28800, f"{ts} % 86400 = {ts % 86400}, not 28800"


def test_all_more_than_48h_from_now():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=48)
    for ts in get_friday_expiries():
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt > cutoff, f"{dt} is within 48h of now ({now})"


def test_deterministic():
    now = datetime(2026, 3, 3, 12, 0, 0, tzinfo=timezone.utc)
    a = get_friday_expiries(now=now)
    b = get_friday_expiries(now=now)
    assert a == b


def test_sorted_ascending():
    result = get_friday_expiries()
    assert result == sorted(result)


def test_friday_before_0800_utc():
    """On a Friday at 07:59 UTC, that Friday should still be >48h away? No,
    it's 0h1m away. So it must be excluded by the 48h cutoff."""
    fri_early = datetime(2026, 3, 6, 7, 59, 0, tzinfo=timezone.utc)
    result = get_friday_expiries(now=fri_early)
    assert len(result) == 3
    for ts in result:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt > fri_early + timedelta(hours=48)


def test_friday_after_0800_utc():
    """On a Friday at 08:01 UTC, that Friday's 08:00 is in the past.
    Next valid Friday is 7 days away — well past 48h cutoff."""
    fri_late = datetime(2026, 3, 6, 8, 1, 0, tzinfo=timezone.utc)
    result = get_friday_expiries(now=fri_late)
    assert len(result) == 3
    for ts in result:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt > fri_late + timedelta(hours=48)


def test_wednesday_cutoff_removes_this_friday():
    """On Wednesday 08:00 UTC, this Friday is only 48h away.
    It should be excluded (cutoff is strictly >48h)."""
    wed = datetime(2026, 3, 4, 8, 0, 0, tzinfo=timezone.utc)
    result = get_friday_expiries(now=wed)
    this_friday_ts = int(datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert this_friday_ts not in result


def test_tuesday_includes_this_friday():
    """On Tuesday 07:00 UTC, this Friday is ~73h away — past cutoff."""
    tue = datetime(2026, 3, 3, 7, 0, 0, tzinfo=timezone.utc)
    result = get_friday_expiries(now=tue)
    this_friday_ts = int(datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert this_friday_ts in result
