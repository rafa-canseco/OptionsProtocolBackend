from datetime import datetime, timezone, timedelta

from src.config import settings
from src.pricing.utils import (
    FRIDAY_WEEKDAY,
    cutoff_hours_for_expiry,
    get_expiries,
    get_target_expiries,
)


def test_returns_at_least_three():
    """At least 3 slots (may be 4 when near_fri != 7d)."""
    result = get_expiries()
    assert len(result) >= 3


class TestCutoffHoursForExpiry:
    def test_short_term_returns_short_cutoff(self):
        now_ts = 1000000
        expiry_ts = now_ts + 24 * 3600  # 24h TTL
        assert cutoff_hours_for_expiry(expiry_ts, now_ts) == 4

    def test_standard_returns_standard_cutoff(self):
        now_ts = 1000000
        expiry_ts = now_ts + 72 * 3600  # 72h TTL
        assert cutoff_hours_for_expiry(expiry_ts, now_ts) == 48

    def test_exact_48h_boundary_uses_short_cutoff(self):
        now_ts = 1000000
        expiry_ts = now_ts + 48 * 3600  # exactly 48h
        assert cutoff_hours_for_expiry(expiry_ts, now_ts) == 4

    def test_just_above_48h_uses_standard_cutoff(self):
        now_ts = 1000000
        expiry_ts = now_ts + 48 * 3600 + 1
        assert cutoff_hours_for_expiry(expiry_ts, now_ts) == 48

    def test_negative_ttl_uses_short_cutoff(self):
        now_ts = 1000000
        expiry_ts = now_ts - 3600  # expired 1h ago
        assert cutoff_hours_for_expiry(expiry_ts, now_ts) == 4


def test_weekly_expiries_are_fridays():
    """The 7d and 14d expiries (last two) must be Fridays."""
    result = get_expiries()
    # Weekly expiries are the two Fridays in the result
    fridays = [
        ts
        for ts in result
        if datetime.fromtimestamp(ts, tz=timezone.utc).weekday() == FRIDAY_WEEKDAY
    ]
    assert len(fridays) >= 2, f"Expected at least 2 Fridays, got {len(fridays)}"


def test_all_satisfy_contract_constraint():
    for ts in get_expiries():
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
    a = get_expiries(now=now)
    b = get_expiries(now=now)
    assert a == b


def test_sorted_ascending():
    result = get_expiries()
    assert result == sorted(result)


def test_friday_before_0800_utc():
    """On a Friday at 07:59, near_fri and 7d collapse to the same
    next Friday → 3 distinct slots (1d Sat, Fri, Fri+7)."""
    fri_early = datetime(2026, 3, 6, 7, 59, 0, tzinfo=timezone.utc)
    result = get_expiries(now=fri_early)
    assert len(result) >= 3


def test_friday_after_0800_utc():
    """On a Friday at 08:01, near_fri and 7d collapse similarly."""
    fri_late = datetime(2026, 3, 6, 8, 1, 0, tzinfo=timezone.utc)
    result = get_expiries(now=fri_late)
    assert len(result) >= 3


def test_wednesday_includes_near_friday():
    """On Wednesday 08:00 UTC, this Friday is 48h away.
    It's within the short cutoff window (4h) so it should be included
    as a near-Friday slot."""
    wed = datetime(2026, 3, 4, 8, 0, 0, tzinfo=timezone.utc)
    result = get_expiries(now=wed)
    this_friday_ts = int(datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert this_friday_ts in result


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
    result = get_expiries(now=tue)
    this_friday_ts = int(datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    assert this_friday_ts in result


class TestTargetExpiriesPolicy:
    def test_default_policy_returns_1d_and_2d_only(self, monkeypatch):
        monkeypatch.setattr(settings, "custom_expiry_timestamps", "")
        monkeypatch.setattr(settings, "target_expiry_tenors", "1d,2d")
        monkeypatch.setattr(settings, "weekly_expiries_enabled", False)

        now = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        result = get_target_expiries(asset="eth", chain="base", now=now)

        assert result == [
            int(datetime(2026, 3, 3, 8, 0, tzinfo=timezone.utc).timestamp()),
            int(datetime(2026, 3, 4, 8, 0, tzinfo=timezone.utc).timestamp()),
        ]

    def test_friday_edge_case_stays_daily_2d_not_biweekly(self, monkeypatch):
        monkeypatch.setattr(settings, "custom_expiry_timestamps", "")
        monkeypatch.setattr(settings, "target_expiry_tenors", "1d,2d")
        monkeypatch.setattr(settings, "weekly_expiries_enabled", False)

        now = datetime(2026, 3, 6, 7, 59, 0, tzinfo=timezone.utc)
        result = get_target_expiries(asset="eth", chain="base", now=now)

        assert result == [
            int(datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc).timestamp()),
            int(datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc).timestamp()),
        ]
        assert all(
            datetime.fromtimestamp(ts, tz=timezone.utc)
            < datetime(2026, 3, 13, 8, 0, tzinfo=timezone.utc)
            for ts in result
        )

    def test_cutoff_pushes_daily_to_next_0800(self, monkeypatch):
        monkeypatch.setattr(settings, "custom_expiry_timestamps", "")
        monkeypatch.setattr(settings, "target_expiry_tenors", "1d,2d")
        monkeypatch.setattr(settings, "weekly_expiries_enabled", False)

        now = datetime(2026, 3, 5, 4, 1, 0, tzinfo=timezone.utc)
        result = get_target_expiries(asset="btc", chain="base", now=now)

        assert result[0] == int(
            datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc).timestamp()
        )
        assert result[1] == int(
            datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc).timestamp()
        )

    def test_weekly_is_feature_flagged(self, monkeypatch):
        monkeypatch.setattr(settings, "custom_expiry_timestamps", "")
        monkeypatch.setattr(settings, "target_expiry_tenors", "1d,2d")
        monkeypatch.setattr(settings, "weekly_expiries_enabled", True)

        now = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        result = get_target_expiries(asset="sol", chain="solana", now=now)

        friday = int(datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc).timestamp())
        assert friday in result

    def test_14d_is_absent_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "custom_expiry_timestamps", "")
        monkeypatch.setattr(settings, "target_expiry_tenors", "1d,2d")
        monkeypatch.setattr(settings, "weekly_expiries_enabled", False)

        now = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        result = get_target_expiries(asset="tslax", chain="solana", now=now)

        biweekly = int(datetime(2026, 3, 13, 8, 0, tzinfo=timezone.utc).timestamp())
        assert biweekly not in result

    def test_custom_expiry_override_still_wins(self, monkeypatch):
        ts1 = int(datetime(2026, 5, 23, 8, 0, tzinfo=timezone.utc).timestamp())
        ts2 = int(datetime(2026, 5, 24, 8, 0, tzinfo=timezone.utc).timestamp())
        monkeypatch.setattr(settings, "custom_expiry_timestamps", f"{ts1},{ts2}")
        monkeypatch.setattr(settings, "target_expiry_tenors", "1d,2d")

        result = get_target_expiries(
            asset="eth",
            chain="base",
            now=datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc),
        )

        assert result == [ts1, ts2]
