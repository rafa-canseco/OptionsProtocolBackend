from datetime import datetime, timezone, timedelta

from src.pricing.utils import get_expiries, get_friday_expiries, FRIDAY_WEEKDAY


def test_returns_at_least_four():
    result = get_expiries()
    assert len(result) >= 4


def test_backward_compat_alias():
    result = get_friday_expiries()
    assert result == get_expiries()


def test_weekly_expiries_are_fridays():
    """The 7d and 14d expiries (last two) must be Fridays."""
    result = get_friday_expiries()
    # Weekly expiries are the two Fridays in the result
    fridays = [
        ts
        for ts in result
        if datetime.fromtimestamp(ts, tz=timezone.utc).weekday() == FRIDAY_WEEKDAY
    ]
    assert len(fridays) >= 2, f"Expected at least 2 Fridays, got {len(fridays)}"


def test_all_satisfy_contract_constraint():
    for ts in get_friday_expiries():
        assert ts % 86400 == 28800, f"{ts} % 86400 = {ts % 86400}, not 28800"


def test_all_past_their_cutoff():
    """Every expiry must be past its dynamic cutoff (4h for 1-day, 48h for standard)."""
    from src.pricing.utils import cutoff_hours_for_expiry

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    for ts in get_expiries():
        cutoff_h = cutoff_hours_for_expiry(ts, now_ts)
        cutoff_dt = now + timedelta(hours=cutoff_h)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt > cutoff_dt, f"{dt} is within {cutoff_h}h cutoff of now ({now})"


def test_deterministic():
    now = datetime(2026, 3, 3, 12, 0, 0, tzinfo=timezone.utc)
    a = get_friday_expiries(now=now)
    b = get_friday_expiries(now=now)
    assert a == b


def test_sorted_ascending():
    result = get_friday_expiries()
    assert result == sorted(result)


def test_friday_before_0800_utc():
    """On a Friday at 07:59, the 1d slot (Saturday 08:00) is ~24h away.
    Standard expiries are all past the 48h cutoff."""
    fri_early = datetime(2026, 3, 6, 7, 59, 0, tzinfo=timezone.utc)
    result = get_expiries(now=fri_early)
    assert len(result) >= 4
    # Standard expiries must be past 48h cutoff
    standard = [ts for ts in result if ts - int(fri_early.timestamp()) > 48 * 3600]
    assert len(standard) >= 3


def test_friday_after_0800_utc():
    """On a Friday at 08:01, next valid Friday is 7 days away.
    1d slot is Saturday 08:00 (~24h)."""
    fri_late = datetime(2026, 3, 6, 8, 1, 0, tzinfo=timezone.utc)
    result = get_expiries(now=fri_late)
    assert len(result) >= 4
    standard = [ts for ts in result if ts - int(fri_late.timestamp()) > 48 * 3600]
    assert len(standard) >= 3


def test_wednesday_cutoff_removes_this_friday():
    """On Wednesday 08:00 UTC, this Friday is only 48h away.
    It should be excluded (cutoff is strictly >48h)."""
    wed = datetime(2026, 3, 4, 8, 0, 0, tzinfo=timezone.utc)
    result = get_friday_expiries(now=wed)
    this_friday_ts = int(datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert this_friday_ts not in result


def test_1day_slot_is_next_day_0800():
    """The 1-day slot is the next 08:00 UTC after the short cutoff (4h)."""
    # At Monday 12:00, short cutoff = 16:00. Next 08:00 = Tuesday 08:00.
    mon = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    result = get_expiries(now=mon)
    tue_0800 = int(datetime(2026, 3, 3, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert tue_0800 in result


def test_1day_slot_deduped_with_standard():
    """When the 1-day and 3-day slots produce the same timestamp, no dupe."""
    # All slots are in a set so duplicates are impossible
    now = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    result = get_expiries(now=now)
    assert len(result) == len(set(result))


def test_tuesday_includes_this_friday():
    """On Tuesday 07:00 UTC, this Friday is ~73h away — past cutoff."""
    tue = datetime(2026, 3, 3, 7, 0, 0, tzinfo=timezone.utc)
    result = get_friday_expiries(now=tue)
    this_friday_ts = int(datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert this_friday_ts in result
