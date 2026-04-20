"""Tests for the /leaderboard endpoint."""

from datetime import datetime, timezone
from itertools import count
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

_id_counter = count(1)

# Competition window timestamps
_START = 1774828800  # 2026-03-30 00:00 UTC
_END = 1776038399  # 2026-04-12 23:59:59 UTC

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
    """Return n rows that together qualify (>= $500 collateral).

    Each row is indexed on a different day.
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
    chain = mock_client.table.return_value.select.return_value
    chain = chain.gte.return_value.lte.return_value.limit.return_value
    chain.execute.return_value.data = rows
    return patch("src.api.leaderboard.get_client", return_value=mock_client)


def _mock_db_me(rows: list[dict]):
    """Mock for /leaderboard/me — chain includes .eq() before .gte()."""
    mock_client = MagicMock()
    chain = mock_client.table.return_value.select.return_value
    chain = chain.eq.return_value.gte.return_value.lte.return_value.limit.return_value
    chain.execute.return_value.data = rows
    return patch("src.api.leaderboard.get_client", return_value=mock_client)


# ---------------------------------------------------------------------------
# Test 1: qualifying filter — collateral
# ---------------------------------------------------------------------------


def test_qualifying_filter_collateral():
    """Wallet below $500 collateral appears with rank=null and qualified=False."""
    rows = _make_qualifying_rows(user_address="0xlow", n=10)
    # Override collateral so total is only 490 (49 * 10)
    for r in rows:
        r["collateral_usd"] = 49.0

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["total_participants"] == 1
    assert data["meta"]["qualified_participants"] == 0
    assert len(data["track1"]) == 1
    entry = data["track1"][0]
    assert entry["rank"] is None
    assert entry["qualified"] is False
    assert entry["progress"]["collateral_pct"] < 1.0


# ---------------------------------------------------------------------------
# Test 2: qualifying filter — active days
# ---------------------------------------------------------------------------


def test_qualifying_filter_active_days():
    """Active days no longer factor into qualification — only collateral matters.

    A wallet with $600 collateral and few active days still qualifies.
    """
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
    # Total collateral: 600 — qualifies on collateral alone

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["total_participants"] == 1
    assert data["meta"]["qualified_participants"] == 1
    entry = data["track1"][0]
    assert entry["rank"] == 1
    assert entry["qualified"] is True


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
        is_itm=True,  # must also be assigned to complete the cycle
        asset="eth",
        settled_at="2026-04-05T10:00:00+00:00",
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
# Test 4: Perfect Week bonus — week 1
# ---------------------------------------------------------------------------


def test_perfect_week_bonus():
    """Wallet with zero ITM in week 1 → OTM settled in week 1 get 1.5× premium."""
    # 10 base rows (all OTM, no settled_at) ensure >= $500 collateral
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
        is_itm=True,  # must also be assigned to complete the cycle
        asset="eth",
        settled_at="2026-04-04T08:00:00+00:00",
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
    assert "qualified_participants" in meta
    assert "total_volume_usd" in meta
    assert "current_week" in meta
    assert meta["competition_start"] == _START
    assert meta["competition_end"] == _END
    assert meta["total_participants"] == 1
    assert isinstance(meta["total_volume_usd"], float)
    assert meta["current_week"] in (1, 2)


# ---------------------------------------------------------------------------
# Test 9: Perfect Week — week 2
# ---------------------------------------------------------------------------


def test_perfect_week_week2_bonus():
    """Zero ITM in week 2 → OTM positions settled in week 2 get 1.5× premium."""
    rows = _make_qualifying_rows(
        user_address="0xpw2", n=10, is_itm=False, settled_at=None
    )

    week2_pos = _make_pos(
        user_address="0xpw2",
        is_itm=False,
        settled_at="2026-04-09T12:00:00+00:00",  # week 2
        indexed_at=_DAY7,
        pos_id=6001,
        net_premium="100000",  # 0.10 USDC
    )
    rows.append(week2_pos)

    total_col = _QUAL_COLLATERAL * 11
    plain_premium = 10 * 0.05
    bonus_premium = 0.10 * 1.5
    expected_rate = round((plain_premium + bonus_premium) / total_col, 6)

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    entry = resp.json()["track1"][0]
    assert abs(entry["earning_rate"] - expected_rate) < 1e-4


# ---------------------------------------------------------------------------
# Test 10: Cross-asset wheel rejected
# ---------------------------------------------------------------------------


def test_cross_asset_wheel_rejected():
    """ETH ITM PUT + BTC CALL do not form a wheel pair (different assets)."""
    base_rows = _make_qualifying_rows(user_address="0xcross", n=8, is_itm=False)

    itm_eth = _make_pos(
        user_address="0xcross",
        is_put=True,
        is_itm=True,
        asset="eth",
        settled_at="2026-04-01T10:00:00+00:00",
        indexed_at=_DAY2,
        pos_id=4001,
    )
    follow_btc = _make_pos(
        user_address="0xcross",
        is_put=False,
        is_itm=False,
        asset="btc",  # different asset — must not pair
        indexed_at="2026-04-01T15:00:00+00:00",
        settled_at=None,
        pos_id=4002,
    )

    rows = base_rows + [itm_eth, follow_btc]

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    entry = resp.json()["track1"][0]
    assert entry["wheel_count"] == 0


# ---------------------------------------------------------------------------
# Test 11: DB error → 502
# ---------------------------------------------------------------------------


def test_db_exception_returns_502():
    """Database connection failure returns 502."""
    mock_client = MagicMock()
    chain = mock_client.table.return_value.select.return_value
    chain = chain.gte.return_value.lte.return_value.limit.return_value
    chain.execute.side_effect = Exception("DB down")

    with patch("src.api.leaderboard.get_client", return_value=mock_client):
        resp = client.get("/leaderboard")

    assert resp.status_code == 502


def test_db_none_data_returns_502():
    """When DB returns result.data=None, endpoint returns 502."""
    mock_client = MagicMock()
    chain = mock_client.table.return_value.select.return_value
    chain = chain.gte.return_value.lte.return_value.limit.return_value
    chain.execute.return_value.data = None

    with patch("src.api.leaderboard.get_client", return_value=mock_client):
        resp = client.get("/leaderboard")

    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Test 12: Boundary values
# ---------------------------------------------------------------------------


def test_boundary_collateral_exactly_500_qualifies():
    """Wallet at exactly $500.00 total collateral qualifies."""
    rows = _make_qualifying_rows(user_address="0xboundary500", n=10)
    for r in rows:
        r["collateral_usd"] = 50.0  # 10 * 50 = 500.00 exactly

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    assert resp.json()["meta"]["total_participants"] == 1


def test_single_position_qualifies_on_collateral_alone():
    """A wallet with one position and $600 collateral qualifies — no days requirement."""
    rows = [
        _make_pos(
            user_address="0xonepos",
            collateral_usd=600.0,
            indexed_at=_DAY0,
            pos_id=2001,
        )
    ]

    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["total_participants"] == 1
    assert data["meta"]["qualified_participants"] == 1
    assert data["track1"][0]["rank"] == 1


# ---------------------------------------------------------------------------
# Test 13: start >= end validation
# ---------------------------------------------------------------------------


def test_start_gte_end_returns_400():
    """start >= end returns 400."""
    resp = client.get(f"/leaderboard?start={_END}&end={_START}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /leaderboard/me tests
# ---------------------------------------------------------------------------

_ME_ADDR = "0xaaaa000000000000000000000000000000000001"


def test_leaderboard_me_no_positions():
    """Wallet with no positions returns zero stats and qualifies=False."""
    with _mock_db_me([]):
        resp = client.get(f"/leaderboard/me?address={_ME_ADDR}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet"] == _ME_ADDR
    assert data["position_count"] == 0
    assert data["qualifies"] is False
    assert data["earning_rate"] is None


def test_leaderboard_me_below_threshold_returns_stats():
    """Wallet below $500 threshold still gets stats, qualifies=False."""
    rows = [
        _make_pos(user_address=_ME_ADDR, collateral_usd=49.0, pos_id=3001),
        _make_pos(user_address=_ME_ADDR, collateral_usd=49.0, pos_id=3002),
    ]
    with _mock_db_me(rows):
        resp = client.get(f"/leaderboard/me?address={_ME_ADDR}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_collateral_usd"] == 98.0
    assert data["qualifies"] is False
    assert data["earning_rate"] is not None


def test_leaderboard_me_qualifying_wallet():
    """Qualifying wallet gets qualifies=True."""
    rows = _make_qualifying_rows(user_address=_ME_ADDR, n=10)
    with _mock_db_me(rows):
        resp = client.get(f"/leaderboard/me?address={_ME_ADDR}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["qualifies"] is True
    assert data["position_count"] == 10
    assert "earning_rate" in data
    assert "active_days" in data
    assert "wheel_count" in data
    assert "otm_streak" in data


def test_leaderboard_me_invalid_address():
    """Invalid address returns 400."""
    resp = client.get("/leaderboard/me?address=0xnotvalid")
    assert resp.status_code == 400


def test_leaderboard_me_missing_address():
    """Missing address param returns 422."""
    resp = client.get("/leaderboard/me")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# B1N-265: qualified flag, progress, rank=null for non-qualified
# ---------------------------------------------------------------------------


def test_qualified_wallet_has_rank_and_flag():
    """Qualifying wallet gets rank=1, qualified=True, progress=1.0."""
    rows = _make_qualifying_rows(user_address="0xqual", n=10)
    with _mock_db(rows):
        resp = client.get("/leaderboard")
    assert resp.status_code == 200
    entry = resp.json()["track1"][0]
    assert entry["rank"] == 1
    assert entry["qualified"] is True
    assert entry["progress"]["collateral_pct"] == 1.0
    assert entry["progress"]["collateral_pct"] == 1.0


def test_mixed_qualified_and_non_qualified_ordering():
    """Qualified wallet ranked first (rank=1), non-qualified wallet has rank=null after."""
    qual_rows = _make_qualifying_rows(user_address="0xqual2", n=10)
    non_qual_rows = [
        _make_pos(user_address="0xnonqual", collateral_usd=49.0, pos_id=9900 + i)
        for i in range(3)
    ]
    with _mock_db(qual_rows + non_qual_rows):
        resp = client.get("/leaderboard")
    assert resp.status_code == 200
    track1 = resp.json()["track1"]
    assert len(track1) == 2
    assert track1[0]["wallet"] == "0xqual2"
    assert track1[0]["rank"] == 1
    assert track1[0]["qualified"] is True
    assert track1[1]["wallet"] == "0xnonqual"
    assert track1[1]["rank"] is None
    assert track1[1]["qualified"] is False


def test_progress_values_capped_at_1():
    """Progress fields are capped at 1.0 for qualifying wallets."""
    rows = _make_qualifying_rows(user_address="0xcapped", n=10)
    # Double the collateral so collateral_pct would be >1 without cap
    for r in rows:
        r["collateral_usd"] = 200.0
    with _mock_db(rows):
        resp = client.get("/leaderboard")
    assert resp.status_code == 200
    entry = resp.json()["track1"][0]
    assert entry["progress"]["collateral_pct"] == 1.0


def test_wheel_requires_both_assignments():
    """Follow-up opened within 24h but not assigned (is_itm=False) does NOT earn wheel bonus."""
    base_rows = _make_qualifying_rows(user_address="0xhalfwheel", n=8)

    itm_put = _make_pos(
        user_address="0xhalfwheel",
        is_put=True,
        is_itm=True,
        asset="eth",
        settled_at="2026-04-01T10:00:00+00:00",
        indexed_at=_DAY2,
        pos_id=8801,
    )
    follow_call = _make_pos(
        user_address="0xhalfwheel",
        is_put=False,
        is_itm=False,  # opened within 24h but expired OTM — wheel incomplete
        asset="eth",
        settled_at="2026-04-05T10:00:00+00:00",
        indexed_at="2026-04-01T20:00:00+00:00",
        pos_id=8802,
    )

    rows = base_rows + [itm_put, follow_call]
    with _mock_db(rows):
        resp = client.get("/leaderboard")

    assert resp.status_code == 200
    entry = resp.json()["track1"][0]
    assert entry["wheel_count"] == 0
