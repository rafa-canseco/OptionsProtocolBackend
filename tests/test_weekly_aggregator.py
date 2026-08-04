from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.bots.weekly_aggregator as aggregator
from tests.weekly_aggregator_oracle import report, wallet_result


class FrozenDateTime(datetime):
    current = datetime(2026, 4, 10, 12, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz else cls.current.replace(tzinfo=None)


class RpcCall:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


class RpcClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        return RpcCall(self.responses.pop(0))

    def table(self, _name):
        pytest.fail("weekly aggregation must not issue raw table reads or writes")


@pytest.mark.parametrize(
    ("now", "expected_start", "expected_end"),
    [
        (
            datetime(2026, 4, 10, 7, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 3, 27, 8, tzinfo=timezone.utc),
            datetime(2026, 4, 3, 8, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
            datetime(2026, 4, 3, 8, tzinfo=timezone.utc),
            datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 4, 11, 17, tzinfo=timezone.utc),
            datetime(2026, 4, 3, 8, tzinfo=timezone.utc),
            datetime(2026, 4, 10, 8, tzinfo=timezone.utc),
        ),
    ],
)
def test_week_boundaries_pin_completed_friday_0800(
    monkeypatch, now, expected_start, expected_end
):
    FrozenDateTime.current = now
    monkeypatch.setattr(aggregator, "datetime", FrozenDateTime)
    assert aggregator._week_boundaries() == (expected_start, expected_end)


@pytest.mark.asyncio
async def test_empty_week_is_one_preflight_no_price_and_no_write(monkeypatch):
    client = RpcClient([{"has_rows": False, "source_rows": 0}])
    prices = AsyncMock()
    monkeypatch.setattr(aggregator, "get_client", lambda: client)
    monkeypatch.setattr(aggregator, "get_eth_price_history", prices)

    assert await aggregator.aggregate_once() is None
    assert [name for name, _ in client.calls] == ["b1nary_legacy_week_source"]
    prices.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonempty_week_uses_exactly_two_fixed_rpcs(monkeypatch):
    result = {
        "status": "aggregated",
        "source_rows": 100_000,
        "wallet_rows": 2_001,
    }
    client = RpcClient([{"has_rows": True, "source_rows": 100_000}, result])
    prices = [
        SimpleNamespace(price=2000.005),
        SimpleNamespace(price=1950.0),
        SimpleNamespace(price=2100.0),
    ]
    monkeypatch.setattr(aggregator, "get_client", lambda: client)
    monkeypatch.setattr(
        aggregator, "get_eth_price_history", AsyncMock(return_value=prices)
    )

    assert await aggregator.aggregate_once() == result
    assert [name for name, _ in client.calls] == [
        "b1nary_legacy_week_source",
        "b1nary_aggregate_legacy_week",
    ]
    write = client.calls[1][1]
    assert write["p_eth_open"] == 2000.005
    assert write["p_eth_close"] == 2100.0
    assert write["p_eth_high"] == 2100.0
    assert write["p_eth_low"] == 1950.0


@pytest.mark.asyncio
async def test_price_failure_does_not_attempt_write(monkeypatch):
    client = RpcClient([{"has_rows": True}])
    monkeypatch.setattr(aggregator, "get_client", lambda: client)
    monkeypatch.setattr(
        aggregator,
        "get_eth_price_history",
        AsyncMock(side_effect=RuntimeError("price unavailable")),
    )

    with pytest.raises(RuntimeError, match="price unavailable"):
        await aggregator.aggregate_once()
    assert [name for name, _ in client.calls] == ["b1nary_legacy_week_source"]


@pytest.mark.asyncio
async def test_empty_price_response_does_not_attempt_write(monkeypatch):
    client = RpcClient([{"has_rows": True}])
    monkeypatch.setattr(aggregator, "get_client", lambda: client)
    monkeypatch.setattr(aggregator, "get_eth_price_history", AsyncMock(return_value=[]))

    with pytest.raises(RuntimeError, match="price history was empty"):
        await aggregator.aggregate_once()
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_wait_until_target_uses_configured_schedule_not_expiry_window(
    monkeypatch,
):
    FrozenDateTime.current = datetime(2026, 4, 10, 11, 30, tzinfo=timezone.utc)
    sleep = AsyncMock()
    monkeypatch.setattr(aggregator, "datetime", FrozenDateTime)
    monkeypatch.setattr(aggregator.asyncio, "sleep", sleep)
    monkeypatch.setattr(aggregator.settings, "weekly_aggregation_day", 4)
    monkeypatch.setattr(aggregator.settings, "weekly_aggregation_hour_utc", 12)

    await aggregator._wait_until_target()
    sleep.assert_awaited_once_with(1800.0)


def test_golden_wallet_math_preserves_fallback_assignment_and_rounding_sequence():
    rows = [
        {"net_premium": 0, "premium": 125_550, "is_settled": False},
        {"net_premium": None, "premium": None, "is_settled": False},
        {
            "net_premium": 200_000,
            "premium": 999_000,
            "is_settled": True,
            "is_itm": True,
            "is_put": True,
            "strike_price": 2100 * 100_000_000,
            "amount": 25_000_000,
        },
        {
            "net_premium": 300_000,
            "is_settled": True,
            "is_itm": True,
            "is_put": False,
            "strike_price": 1900 * 100_000_000,
            "amount": 10_000_000,
        },
        {
            "net_premium": 50_000,
            "is_settled": True,
            "is_itm": False,
            "is_put": True,
            "strike_price": 5000 * 100_000_000,
            "amount": 100_000_000,
        },
        {
            "net_premium": 10_000,
            "is_settled": True,
            "is_itm": True,
            "is_put": True,
            "strike_price": None,
            "amount": 100_000_000,
        },
    ]
    actual = wallet_result("0xabc", rows, eth_close=2000.0, previous=1.23456)
    assert actual == {
        "user_address": "0xabc",
        "positions_opened": 6,
        "total_simulated_premium": 0.6856,
        "assignments": 3,
        "simulated_pnl": -34.3145,
        "cumulative_pnl": -33.0799,
    }


def test_golden_report_includes_all_nonempty_wallets_and_narrative_values():
    rows = [
        {"user_address": "0xAA", "net_premium": 100_000},
        {"user_address": "0xaa", "net_premium": 200_000},
        {
            "user_address": "0xBB",
            "net_premium": 500_000,
            "is_settled": True,
            "is_itm": True,
            "is_put": True,
            "strike_price": 2000 * 100_000_000,
            "amount": 100_000_000,
        },
        {"user_address": "", "net_premium": 9_000_000},
    ]
    users, actual = report(
        rows,
        eth_open=1900.0,
        eth_close=1950.0,
        eth_high=2000.005,
        eth_low=1800.005,
    )
    assert {item["user_address"] for item in users} == {"0xaa", "0xbb"}
    assert actual == {
        "total_users": 2,
        "total_positions": 4,
        "total_simulated_premium": 0.8,
        "total_assignments": 1,
        "eth_open": 1900.0,
        "eth_close": 1950.0,
        "eth_high": 2000.01,
        "eth_low": 1800.01,
        "narrative_data": {
            "highest_premium_earned": 0.5,
            "most_active_positions": 2,
            "total_unique_users": 2,
            "eth_week_change_pct": 2.63,
            "users_with_assignments": 1,
        },
    }


def test_binary64_ties_preserve_ordered_premium_loss_cumulative_and_report_adds():
    wallet_rows = [
        {"net_premium": 361_746},
        {"net_premium": 735_104},
        {
            "net_premium": 0,
            "premium": 0,
            "is_settled": True,
            "is_itm": True,
            "is_put": True,
            "strike_price": 2100 * 100_000_000,
            "amount": 1_000_000,
        },
    ]
    assert wallet_result("0xabc", wallet_rows, eth_close=2000.0, previous=0.00005) == {
        "user_address": "0xabc",
        "positions_opened": 3,
        "total_simulated_premium": 1.0968,
        "assignments": 1,
        "simulated_pnl": 0.0968,
        "cumulative_pnl": 0.0969,
    }

    users, summary = report(
        [
            {"user_address": "0xscale", "net_premium": 8_800_250_000},
            {"user_address": "0xtie", "net_premium": 1_096_800},
            {
                "user_address": "0xhuge",
                "net_premium": 100_000_000_000_000_000,
            },
            *[
                {"user_address": f"0xtiny{index}", "net_premium": 100}
                for index in range(8)
            ],
        ],
        eth_open=2000.0,
        eth_close=2000.0,
        eth_high=2000.0,
        eth_low=2000.0,
    )
    wallet_totals = [item["total_simulated_premium"] for item in users]
    exact_total = sum(Decimal(str(value)) for value in wallet_totals)
    reverse_total = round(sum(reversed(wallet_totals)), 4)
    assert summary["total_simulated_premium"] == 100000008801.3477
    assert exact_total == Decimal("100000008801.3476")
    assert reverse_total == 100000008801.3476
    assert summary["total_simulated_premium"] != float(exact_total)
    assert summary["total_simulated_premium"] != reverse_total


def test_golden_nonpositive_call_and_put_losses_do_not_reduce_pnl():
    rows = [
        {
            "net_premium": 100_000,
            "is_settled": True,
            "is_itm": True,
            "is_put": True,
            "strike_price": 1900 * 100_000_000,
            "amount": 100_000_000,
        },
        {
            "net_premium": 100_000,
            "is_settled": True,
            "is_itm": True,
            "is_put": False,
            "strike_price": 2100 * 100_000_000,
            "amount": 100_000_000,
        },
    ]
    assert (
        wallet_result("wallet", rows, eth_close=2000, previous=0)["simulated_pnl"]
        == 0.2
    )
