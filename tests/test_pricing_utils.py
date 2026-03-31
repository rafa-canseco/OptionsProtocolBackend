"""Unit tests for src/pricing/utils.py — collateral_to_usd."""

import pytest

from src.pricing.utils import collateral_to_usd

ETH_SPOT = 3000.0
BTC_SPOT = 90000.0


def _row(collateral: str, is_put: bool | None, asset: str = "eth") -> dict:
    return {"collateral": collateral, "is_put": is_put, "asset": asset}


class TestCollateralToUsd:
    def test_put_usdc_1_dollar(self):
        """1 USDC (1e6 raw) → $1.00."""
        assert collateral_to_usd(
            _row("1000000", True), ETH_SPOT, BTC_SPOT
        ) == pytest.approx(1.0)

    def test_put_usdc_5_dollars(self):
        """5 USDC → $5.00."""
        assert collateral_to_usd(
            _row("5000000", True), ETH_SPOT, BTC_SPOT
        ) == pytest.approx(5.0)

    def test_put_is_put_none_treated_as_put(self):
        """is_put=None falls back to PUT (USDC) path, not spot-price path."""
        result = collateral_to_usd(_row("2000000", None), ETH_SPOT, BTC_SPOT)
        assert result == pytest.approx(2.0)

    def test_eth_call_1_weth(self):
        """1 WETH (1e18 raw) at $3000 → $3000."""
        raw = str(10**18)
        assert collateral_to_usd(
            _row(raw, False, "eth"), ETH_SPOT, BTC_SPOT
        ) == pytest.approx(3000.0)

    def test_btc_call_1_btc(self):
        """1 cbBTC (1e8 raw) at $90000 → $90000."""
        raw = str(10**8)
        assert collateral_to_usd(
            _row(raw, False, "btc"), ETH_SPOT, BTC_SPOT
        ) == pytest.approx(90000.0)

    def test_eth_call_unknown_asset_falls_back_to_eth(self):
        """Unknown asset string defaults to ETH path (1e18 decimals)."""
        raw = str(10**18)
        row = {"collateral": raw, "is_put": False, "asset": "unknown"}
        assert collateral_to_usd(row, ETH_SPOT, BTC_SPOT) == pytest.approx(ETH_SPOT)

    def test_zero_collateral_returns_zero(self):
        """Zero collateral produces 0.0 for all option types."""
        assert collateral_to_usd(_row("0", True), ETH_SPOT, BTC_SPOT) == pytest.approx(
            0.0
        )
        assert collateral_to_usd(
            _row("0", False, "eth"), ETH_SPOT, BTC_SPOT
        ) == pytest.approx(0.0)
        assert collateral_to_usd(
            _row("0", False, "btc"), ETH_SPOT, BTC_SPOT
        ) == pytest.approx(0.0)

    def test_missing_collateral_returns_zero(self):
        """Missing or None collateral treated as 0."""
        assert collateral_to_usd(
            {"collateral": None, "is_put": True}, 0.0, 0.0
        ) == pytest.approx(0.0)
        assert collateral_to_usd({}, 0.0, 0.0) == pytest.approx(0.0)
