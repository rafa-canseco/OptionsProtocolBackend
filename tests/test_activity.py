"""Tests for GET /activity/{wallet_address}."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

VALID_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
ALSO_ADDRESS = "0xabcdef1234567890abcdef1234567890abcdef12"
UNKNOWN_ADDRESS = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _make_row(
    *,
    collateral: str = "1000000",  # 1 USDC
    net_premium: str = "50000",  # 0.05 USDC
    is_put: bool = True,
    strike_price: int = 0,
    asset: str = "eth",
    days_ago: int = 0,
    id: str = "abc123",
) -> dict:
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()
    collateral_usd = int(collateral) / 1_000_000 if is_put else 0.0
    return {
        "id": id,
        "collateral": collateral,
        "net_premium": net_premium,
        "premium": net_premium,
        "is_put": is_put,
        "strike_price": strike_price,
        "asset": asset,
        "indexed_at": ts,
        "collateral_usd": collateral_usd,
    }



def _mock_db_in(rows: list[dict]):
    """Return a mock get_client() that yields the given rows via .in_() chain."""
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.data = rows
    (
        mock_client.table.return_value.select.return_value.in_.return_value.execute.return_value
    ) = mock_result
    return mock_client


def test_activity_unknown_wallet():
    """Wallet with no activity returns zeroes."""
    with patch("src.api.activity.get_client", return_value=_mock_db_in([])):
        resp = client.get(f"/activity/{UNKNOWN_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet"] == UNKNOWN_ADDRESS.lower()
    assert data["totalVolume"] == 0.0
    assert data["totalPremiumEarned"] == 0.0
    assert data["positionCount"] == 0
    assert data["activeDays"] == 0
    assert data["daysSinceFirst"] == 0
    assert "activeReferrals" not in data


def test_activity_invalid_address():
    resp = client.get("/activity/0xnotanaddress")
    assert resp.status_code == 400


def test_activity_no_0x_prefix():
    resp = client.get("/activity/1234567890abcdef1234567890abcdef12345678")
    assert resp.status_code == 400


def test_activity_single_position():
    """Single put position: volume = 1 USDC, premium = 0.05 USDC."""
    rows = [_make_row(collateral="1000000", net_premium="50000", is_put=True)]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["positionCount"] == 1
    assert data["totalVolume"] == pytest.approx(1.0)
    assert data["totalPremiumEarned"] == pytest.approx(0.05)
    assert data["activeDays"] == 1
    assert data["daysSinceFirst"] == 0


def test_activity_multiple_positions_same_day():
    """Two positions on the same day count as 1 active day."""
    rows = [
        _make_row(
            collateral="2000000", net_premium="100000", is_put=True, days_ago=0, id="r1"
        ),
        _make_row(
            collateral="3000000", net_premium="150000", is_put=True, days_ago=0, id="r2"
        ),
    ]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["positionCount"] == 2
    assert data["activeDays"] == 1
    assert data["totalVolume"] == pytest.approx(5.0)
    assert data["totalPremiumEarned"] == pytest.approx(0.25)


def test_activity_multiple_days():
    """Positions on different days produce correct activeDays and daysSinceFirst."""
    rows = [
        _make_row(days_ago=0, id="d0"),
        _make_row(days_ago=3, id="d3a"),
        _make_row(days_ago=3, id="d3b"),  # duplicate day — still counts as 1
        _make_row(days_ago=7, id="d7"),
    ]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["activeDays"] == 3  # day 0, day 3, day 7
    assert data["daysSinceFirst"] == 7
    assert data["positionCount"] == 4


def test_activity_eth_call_volume_in_usd():
    """ETH call: 1 WETH collateral at $2500 strike → $2500 USD volume."""
    weth_raw = str(10**18)  # 1 WETH
    strike = 250000000000  # $2500 with 8 decimals
    rows = [
        _make_row(collateral=weth_raw, is_put=False, strike_price=strike, asset="eth")
    ]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    assert resp.json()["totalVolume"] == pytest.approx(2500.0)


def test_activity_btc_call_volume_in_usd():
    """BTC call: 1 cbBTC collateral at $90000 strike → $90000 USD volume."""
    btc_raw = str(10**8)  # 1 cbBTC (8 decimals)
    strike = 9000000000000  # $90000 with 8 decimals
    rows = [
        _make_row(collateral=btc_raw, is_put=False, strike_price=strike, asset="btc")
    ]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    assert resp.json()["totalVolume"] == pytest.approx(90000.0)


def test_activity_premium_fallback_to_gross():
    """Rows without net_premium fall back to gross premium field."""
    row = _make_row()
    row["net_premium"] = None
    row["premium"] = "200000"  # 0.20 USDC
    with patch("src.api.activity.get_client", return_value=_mock_db_in([row])):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    assert resp.json()["totalPremiumEarned"] == pytest.approx(0.20)


def test_activity_wallet_lowercased():
    """Wallet address is normalized to lowercase in response."""
    mixed = "0xAbCd" + "0" * 36
    with patch("src.api.activity.get_client", return_value=_mock_db_in([])):
        resp = client.get(f"/activity/{mixed}")
    assert resp.status_code == 200
    assert resp.json()["wallet"] == mixed.lower()


# --- New tests ---


def test_also_param_aggregates_two_addresses():
    """Two distinct rows from two addresses are combined into positionCount=2."""
    rows = [
        _make_row(collateral="1000000", net_premium="50000", is_put=True, id="r1"),
        _make_row(collateral="2000000", net_premium="100000", is_put=True, id="r2"),
    ]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}?also={ALSO_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["positionCount"] == 2
    assert data["totalVolume"] == pytest.approx(3.0)
    assert data["totalPremiumEarned"] == pytest.approx(0.15)


def test_also_deduplicate_by_id():
    """Same id from both addresses is counted once, positionCount=1."""
    shared_row = _make_row(collateral="1000000", net_premium="50000", is_put=True, id="shared")
    rows = [shared_row, shared_row]  # same row returned twice (both addresses match)
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}?also={ALSO_ADDRESS}")
    assert resp.status_code == 200
    assert resp.json()["positionCount"] == 1


def test_also_invalid_address_returns_400():
    """Invalid ?also= address returns 400."""
    resp = client.get(f"/activity/{VALID_ADDRESS}?also=0xnotvalid")
    assert resp.status_code == 400


def test_earning_rate_calculation():
    """2 USDC collateral_usd and 0.10 USDC premium → earning_rate ≈ 0.05."""
    rows = [
        _make_row(collateral="2000000", net_premium="100000", is_put=True, id="e1"),
    ]
    # Override collateral_usd to exactly 2.0
    rows[0]["collateral_usd"] = 2.0
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["earning_rate"] == pytest.approx(0.05)


def test_earning_rate_zero_collateral_is_null():
    """When all collateral_usd is 0 or None, earning_rate is null (None)."""
    rows = [
        _make_row(collateral="1000000", net_premium="50000", is_put=True, id="z1"),
    ]
    rows[0]["collateral_usd"] = 0
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    assert resp.json()["earning_rate"] is None


def test_new_fields_present():
    """Response includes total_collateral_usd, total_premium_usd, earning_rate."""
    rows = [_make_row(collateral="1000000", net_premium="50000", is_put=True)]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_collateral_usd" in data
    assert "total_premium_usd" in data
    assert "earning_rate" in data


def test_backward_compat_no_also():
    """Without ?also=, existing fields are unchanged."""
    rows = [_make_row(collateral="1000000", net_premium="50000", is_put=True)]
    with patch("src.api.activity.get_client", return_value=_mock_db_in(rows)):
        resp = client.get(f"/activity/{VALID_ADDRESS}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["positionCount"] == 1
    assert data["totalVolume"] == pytest.approx(1.0)
    assert data["totalPremiumEarned"] == pytest.approx(0.05)
    assert data["activeDays"] == 1
    assert data["daysSinceFirst"] == 0
