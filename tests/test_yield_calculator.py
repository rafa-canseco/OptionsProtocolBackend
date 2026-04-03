"""Tests for the yield calculator — time-weighted pro-rata allocation."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from src.yield_tracking.calculator import calculate_allocations


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


_PERIOD_START = _dt("2026-04-02T00:00:00+00:00")
_PERIOD_END = _dt("2026-04-13T00:00:00+00:00")
_FULL_DURATION = (_PERIOD_END - _PERIOD_START).total_seconds()


def _mock_positions(rows: list[dict]):
    mock_client = MagicMock()
    chain = mock_client.table.return_value.select.return_value
    chain = chain.eq.return_value.lt.return_value
    chain.execute.return_value.data = rows
    return patch("src.yield_tracking.calculator.get_client", return_value=mock_client)


def test_single_position_gets_full_share():
    """One position active for the entire period gets 96% of yield (after 4% fee)."""
    positions = [
        {
            "id": "pos-1",
            "user_address": "0xaaa",
            "collateral_amount": 1000_000_000,  # 1000 USDC
            "deposited_at": "2026-04-01T00:00:00+00:00",  # before period
            "settled_at": None,
        }
    ]
    with _mock_positions(positions):
        allocations, fee = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "usdc", 100_000_000
        )

    assert len(allocations) == 1
    assert allocations[0]["user_address"] == "0xaaa"
    # 100M * 96% = 96M
    assert allocations[0]["amount"] == 96_000_000
    assert fee == 4_000_000


def test_two_positions_equal_weight():
    """Two positions with same collateral and full duration split evenly."""
    positions = [
        {
            "id": "pos-1",
            "user_address": "0xaaa",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        },
        {
            "id": "pos-2",
            "user_address": "0xbbb",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        },
    ]
    with _mock_positions(positions):
        allocations, fee = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "usdc", 100_000
        )

    assert len(allocations) == 2
    assert fee == 4_000  # 4%
    total_allocated = sum(a["amount"] for a in allocations)
    # 96000 split evenly = 48000 each
    assert total_allocated == 96_000
    assert allocations[0]["amount"] == 48_000
    assert allocations[1]["amount"] == 48_000


def test_partial_duration_gets_proportional_share():
    """Position active for half the period gets half the weight."""
    midpoint = "2026-04-07T12:00:00+00:00"  # roughly halfway
    positions = [
        {
            "id": "pos-full",
            "user_address": "0xaaa",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        },
        {
            "id": "pos-half",
            "user_address": "0xbbb",
            "collateral_amount": 1000,
            "deposited_at": midpoint,
            "settled_at": None,
        },
    ]
    with _mock_positions(positions):
        allocations, _ = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "eth", 1000
        )

    assert len(allocations) == 2
    # pos-full has more weight (full duration) than pos-half
    full_alloc = next(a for a in allocations if a["user_address"] == "0xaaa")
    half_alloc = next(a for a in allocations if a["user_address"] == "0xbbb")
    assert full_alloc["amount"] > half_alloc["amount"]


def test_settled_position_only_counts_active_time():
    """Position settled mid-period only gets weight for active duration."""
    positions = [
        {
            "id": "pos-settled",
            "user_address": "0xaaa",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": "2026-04-05T00:00:00+00:00",  # 3 days into period
        },
        {
            "id": "pos-active",
            "user_address": "0xbbb",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        },
    ]
    with _mock_positions(positions):
        allocations, _ = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "usdc", 10000
        )

    active_alloc = next(a for a in allocations if a["user_address"] == "0xbbb")
    settled_alloc = next(a for a in allocations if a["user_address"] == "0xaaa")
    assert active_alloc["amount"] > settled_alloc["amount"]


def test_position_settled_before_period_excluded():
    """Position settled before the period starts should be excluded."""
    positions = [
        {
            "id": "pos-old",
            "user_address": "0xaaa",
            "collateral_amount": 1000,
            "deposited_at": "2026-03-15T00:00:00+00:00",
            "settled_at": "2026-03-25T00:00:00+00:00",  # before period
        },
        {
            "id": "pos-active",
            "user_address": "0xbbb",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        },
    ]
    with _mock_positions(positions):
        allocations, _ = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "usdc", 10000
        )

    # Only pos-active should get an allocation
    assert len(allocations) == 1
    assert allocations[0]["user_address"] == "0xbbb"


def test_no_positions_returns_empty():
    """No positions → no allocations."""
    with _mock_positions([]):
        allocations, fee = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "usdc", 10000
        )

    assert allocations == []
    assert fee == 0


def test_higher_collateral_gets_more():
    """Position with 3x collateral gets 3x the allocation."""
    positions = [
        {
            "id": "pos-small",
            "user_address": "0xaaa",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        },
        {
            "id": "pos-big",
            "user_address": "0xbbb",
            "collateral_amount": 3000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        },
    ]
    with _mock_positions(positions):
        allocations, _ = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "usdc", 40000
        )

    small = next(a for a in allocations if a["user_address"] == "0xaaa")
    big = next(a for a in allocations if a["user_address"] == "0xbbb")
    assert big["amount"] == small["amount"] * 3


def test_fee_is_4_percent():
    """Platform fee is exactly 4% of total yield."""
    positions = [
        {
            "id": "pos-1",
            "user_address": "0xaaa",
            "collateral_amount": 1000,
            "deposited_at": "2026-04-01T00:00:00+00:00",
            "settled_at": None,
        }
    ]
    total_yield = 1_000_000
    with _mock_positions(positions):
        allocations, fee = calculate_allocations(
            "dist-1", _PERIOD_START, _PERIOD_END, "usdc", total_yield
        )

    assert fee == 40_000  # 4%
    assert allocations[0]["amount"] == 960_000  # 96%
