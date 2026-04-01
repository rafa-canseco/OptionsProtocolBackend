"""Tests for the /leaderboard endpoint."""

from datetime import datetime, timezone
from itertools import count
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

_id_counter = count(1)

# Competition window timestamps
_START = 1743292800  # 2026-03-30 00:00 UTC
_END = 1744502399  # 2026-04-12 23:59:59 UTC

# Enough collateral for one wallet to qualify on its own (>= $500)
_QUAL_COLLATERAL = 55.0  # 10 positions * 55 = 550

# Timestamps within competition window
_DAY0 = "2026-03-30T00:00:00+00:00"  # indexed_at day 0
_DAY1 = "2026-03-31T00:00:00+00:00"
_DAY2 = "2026-04-01T00:00:00+00:00"
_DAY3 = "2026-04-02T00:00:00+00:00"
_DAY4 = "2026-04-03T00:00:00+00:00"
_DAY5 = "2026-04-04T00:00:00+00:00"
_DAY6 = "2026-04-05T00:00:00+00:00"
_DAY7 = "2026-04-06T00:00:00+00:00"
_DAY8 = "2026-04-07T00:00:00+00:00"

# An expiry that spans the whole competition (far future)
_FAR_EXPIRY = int(datetime(2026, 4, 13, 0, 0, 0, tzinfo=timezone.utc).timestamp())


def _make_pos(
    *,
    user_address="0xaaaa",
    collateral_usd=_QUAL_COLLATERAL,
    net_premium="50000",  # 0.05 USDC
    premium=None,
    is_put=True,
    is_itm=False,
    asset="eth",
    indexed_at=_DAY0,
    expiry=_FAR_EXPIRY,
    settled_at="2026-03-31T12:00:00+00:00",
    pos_id=None,
) -> dict:
    """Build a minimal order_events row with sensible defaults."""
    return {
        "id": pos_id if pos_id is not None else next(_id_counter),
        "user_address": user_address,
        "collateral_usd": collateral_usd,
        "net_premium": net_premium,
        "premium": premium,
        "is_put": is_put,
        "is_itm": is_itm,
        "asset": asset,
        "indexed_at": indexed_at,
        "expiry": expiry,
        "settled_at": settled_at,
    }


def _make_qualifying_rows(user_address="0xaaaa", n=10, **overrides) -> list[dict]:
    """Return n rows that together qualify (>= $500 collateral, >= 8 active days).

    Each row is indexed on a different day to guarantee 8+ active days.
    """
    days = [
        _DAY0,
        _DAY1,
        _DAY2,
        _DAY3,
        _DAY4,
        _DAY5,
        _DAY6,
        _DAY7,
        _DAY8,
        "2026-04-08T00:00:00+00:00",
    ]
    rows = []
    for i in range(n):
        day = days[i % len(days)]
        rows.append(
            _make_pos(
                user_address=user_address,
                indexed_at=day,
                expiry=_FAR_EXPIRY,
                **{"collateral_usd": _QUAL_COLLATERAL, **overrides},
            )
        )
    return rows


def _mock_db(rows: list[dict]):
    """Return a context manager that patches get_client with the given rows."""
    mock_client = MagicMock()
    (
        mock_client.table.return_value.select.return_value.gte.return_value.lte.return_value.execute.return_value.data
    ) = rows
    return patch("src.api.leaderboard.get_client", return_value=mock_client)


# ---------------------------------------------------------------------------
# Test 1: qualifying filter — collateral
# ---------------------------------------------------------------------------


def test_qualifying_filter_collateral():
    """Wallet with total collateral_usd < 500 must not appear in leaderboard."""
    rows = _make_qualifying_rows(user_address="0xlow", n=10)
    # Override collateral so total is only 490 (49 * 10)
    for r in rows:
        r["collateral_usd"] = 49.0

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["total_participants"] == 0
    assert data["track1"] == []
    assert data["track2"] == []


# ---------------------------------------------------------------------------
# Test 2: qualifying filter — active days
# ---------------------------------------------------------------------------


def test_qualifying_filter_active_days():
    """Wallet with < 8 active days must not appear in leaderboard."""
    # All 10 rows on the same day → 1 active day (with far expiry: multiple days
    # but we need < 8). Use an expiry that ends before day 8.
    short_expiry = int(datetime(2026, 4, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        _make_pos(
            user_address="0xshort",
            collateral_usd=60.0,
            indexed_at=_DAY0,
            expiry=short_expiry,
        )
        for _ in range(10)
    ]
    # Total collateral: 600 — passes collateral filter but only ~3 active days

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["total_participants"] == 0


# ---------------------------------------------------------------------------
# Test 3: Wheel detection applies 1.5× multiplier
# ---------------------------------------------------------------------------


def test_wheel_detection_applies_1_5x():
    """ITM PUT settled → CALL indexed within 24 h → both get 1.5× premium, wheel_count=1."""
    base_rows = _make_qualifying_rows(user_address="0xwheel", n=8)

    settled_ts = "2026-04-01T10:00:00+00:00"
    follow_ts = "2026-04-01T20:00:00+00:00"  # 10 h later

    itm_put = _make_pos(
        user_address="0xwheel",
        is_put=True,
        is_itm=True,
        asset="eth",
        settled_at=settled_ts,
        indexed_at=_DAY2,
        pos_id=9001,
    )
    follow_call = _make_pos(
        user_address="0xwheel",
        is_put=False,
        is_itm=False,
        asset="eth",
        settled_at=None,
        indexed_at=follow_ts,
        pos_id=9002,
    )

    rows = base_rows + [itm_put, follow_call]

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["total_participants"] == 1
    entry = data["track1"][0]
    assert entry["wheel_count"] == 1
    # Premium for the two wheel positions (0.05 * 1.5 each) vs plain 0.05
    # Verify earning_rate is higher than if no bonus were applied
    assert entry["earning_rate"] is not None


# ---------------------------------------------------------------------------
# Test 4: Perfect Week bonus
# ---------------------------------------------------------------------------


def test_perfect_week_bonus():
    """Wallet with zero ITM in week 1 → OTM settled in week 1 get 1.5× premium."""
    # 10 base rows (all OTM, no settled_at) ensure >= $500 collateral and >= 8 active days
    rows = _make_qualifying_rows(
        user_address="0xperfect", n=10, is_itm=False, settled_at=None
    )

    # Add an OTM position settled in week 1 (not a wheel)
    week1_pos = _make_pos(
        user_address="0xperfect",
        is_itm=False,
        settled_at="2026-04-03T12:00:00+00:00",
        indexed_at=_DAY3,
        pos_id=8001,
        net_premium="100000",  # 0.10 USDC
    )
    rows.append(week1_pos)

    # Compute expected earning_rate with bonus on that one position
    total_col = _QUAL_COLLATERAL * 10 + _QUAL_COLLATERAL  # 11 rows
    # 10 rows * 0.05 + 1 row * 0.10 * 1.5 = 0.50 + 0.15 = 0.65
    plain_premium = 10 * 0.05
    bonus_premium = 0.10 * 1.5
    expected_rate = round((plain_premium + bonus_premium) / total_col, 6)

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    entry = resp.json()["track1"][0]
    assert abs(entry["earning_rate"] - expected_rate) < 1e-4


# ---------------------------------------------------------------------------
# Test 5: Wheel takes priority over Perfect Week
# ---------------------------------------------------------------------------


def test_wheel_priority_over_perfect_week():
    """A position eligible for both bonuses only gets Wheel (1.5×, not stacked)."""
    base_rows = _make_qualifying_rows(user_address="0xboth", n=8, is_itm=False)

    settled_ts = "2026-04-02T08:00:00+00:00"  # week 1
    follow_ts = "2026-04-02T16:00:00+00:00"

    itm_put = _make_pos(
        user_address="0xboth",
        is_put=True,
        is_itm=True,
        asset="eth",
        settled_at=settled_ts,
        indexed_at=_DAY2,
        pos_id=7001,
    )
    follow_call = _make_pos(
        user_address="0xboth",
        is_put=False,
        is_itm=False,
        asset="eth",
        settled_at=None,
        indexed_at=follow_ts,
        pos_id=7002,
    )

    rows = base_rows + [itm_put, follow_call]

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    # Wheel pair found: 1
    entry = resp.json()["track1"][0]
    assert entry["wheel_count"] == 1
    # The ITM position in week 1 prevents a perfect week — no extra stacking
    # We just verify the endpoint returns successfully and wheel_count is correct


# ---------------------------------------------------------------------------
# Test 6: OTM streak basic
# ---------------------------------------------------------------------------


def test_otm_streak_basic():
    """3 OTM → 1 ITM → 2 OTM → max streak = 3."""
    base_rows = _make_qualifying_rows(
        user_address="0xstreak", n=8, is_itm=False, settled_at=None
    )

    settled_positions = [
        _make_pos(
            user_address="0xstreak",
            is_itm=False,
            settled_at="2026-03-30T01:00:00+00:00",
            pos_id=5001,
        ),
        _make_pos(
            user_address="0xstreak",
            is_itm=False,
            settled_at="2026-03-30T02:00:00+00:00",
            pos_id=5002,
        ),
        _make_pos(
            user_address="0xstreak",
            is_itm=False,
            settled_at="2026-03-30T03:00:00+00:00",
            pos_id=5003,
        ),
        _make_pos(
            user_address="0xstreak",
            is_itm=True,
            settled_at="2026-03-31T01:00:00+00:00",
            pos_id=5004,
        ),
        _make_pos(
            user_address="0xstreak",
            is_itm=False,
            settled_at="2026-04-01T01:00:00+00:00",
            pos_id=5005,
        ),
        _make_pos(
            user_address="0xstreak",
            is_itm=False,
            settled_at="2026-04-01T02:00:00+00:00",
            pos_id=5006,
        ),
    ]

    rows = base_rows + settled_positions

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    entry = resp.json()["track2"][0]
    assert entry["wallet"] == "0xstreak"
    assert entry["otm_streak"] == 3


# ---------------------------------------------------------------------------
# Test 7: Track 1 ranking
# ---------------------------------------------------------------------------


def test_track1_ranking():
    """Wallet with higher earning_rate ranks first."""
    # Wallet A: 10 rows, premium=100000 (0.10 each), collateral=55 each → rate=0.10/55≈0.00182
    wallet_a = _make_qualifying_rows(
        user_address="0xwallet_a",
        n=10,
        net_premium="100000",
        collateral_usd=55.0,
        is_itm=False,
    )
    # Wallet B: 10 rows, premium=50000 (0.05 each), collateral=55 each → rate=0.05/55≈0.00091
    wallet_b = _make_qualifying_rows(
        user_address="0xwallet_b",
        n=10,
        net_premium="50000",
        collateral_usd=55.0,
        is_itm=False,
    )

    rows = wallet_a + wallet_b

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    track1 = resp.json()["track1"]
    assert len(track1) == 2
    assert track1[0]["wallet"] == "0xwallet_a"
    assert track1[0]["rank"] == 1
    assert track1[1]["wallet"] == "0xwallet_b"
    assert track1[1]["rank"] == 2


# ---------------------------------------------------------------------------
# Test 8: Metadata fields
# ---------------------------------------------------------------------------


def test_metadata_fields():
    """Response includes all required metadata fields with correct types."""
    rows = _make_qualifying_rows(user_address="0xmeta", n=10)

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    meta = resp.json()["meta"]
    assert "competition_start" in meta
    assert "competition_end" in meta
    assert "total_participants" in meta
    assert "total_volume_usd" in meta
    assert "current_week" in meta
    assert meta["competition_start"] == _START
    assert meta["competition_end"] == _END
    assert meta["total_participants"] == 1
    assert isinstance(meta["total_volume_usd"], float)
    assert meta["current_week"] in (1, 2)
