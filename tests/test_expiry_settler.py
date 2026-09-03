"""Tests for expiry_settler financial math functions.

Only tests pure math functions — no DB, no RPC, no chain.
External dependencies (Quoter, oracle) are mocked at the module boundary.
"""

import asyncio

import pytest
from unittest.mock import MagicMock, patch

import src.bots.expiry_settler as settler_module
from src.bots.expiry_settler import (
    BETA_SLIPPAGE_BPS,
    _beta_compute_max_collateral_put,
    _compute_contra_amount,
    _compute_min_amount_out,
    _ensure_expiry_prices_set,
    _physical_redeem_with_retry,
    _post_settle_sweep,
    _reconcile_settled_on_chain,
    compute_slippage_param,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_position(
    amount_raw: int = 100_000_000, strike: int = 250_000_000_000
) -> dict:
    """Return a minimal CALL position dict (1 oToken at $2500 strike)."""
    return {
        "amount": str(amount_raw),
        "strike_price": str(strike),
        "is_put": False,
        "otoken_address": "0xCALL",
        "asset": "eth",
    }


def _put_position(amount_raw: int = 100_000_000, strike: int = 250_000_000_000) -> dict:
    """Return a minimal PUT position dict (1 oToken at $2500 strike)."""
    return {
        "amount": str(amount_raw),
        "strike_price": str(strike),
        "is_put": True,
        "otoken_address": "0xPUT",
        "asset": "eth",
    }


class _QueryRecorder:
    def __init__(self):
        self.calls = []
        self.not_ = self
        self.data = []

    def table(self, *args):
        self.calls.append(("table", args))
        return self

    def select(self, *args):
        self.calls.append(("select", args))
        return self

    def eq(self, *args):
        self.calls.append(("eq", args))
        return self

    def is_(self, *args):
        self.calls.append(("is_", args))
        return self

    def or_(self, *args):
        self.calls.append(("or_", args))
        return self

    def lte(self, *args):
        self.calls.append(("lte", args))
        return self

    def execute(self):
        return self


# ---------------------------------------------------------------------------
# Base settlement DB queries
# ---------------------------------------------------------------------------


class TestBaseSettlementQueries:
    def test_get_expired_unsettled_filters_base_chain(self):
        db = _QueryRecorder()

        with patch("src.bots.expiry_settler.get_client", return_value=db):
            settler_module.get_expired_unsettled()

        assert ("eq", ("chain", "base")) in db.calls

    def test_get_pending_phase2_filters_base_chain(self):
        db = _QueryRecorder()

        with patch("src.bots.expiry_settler.get_client", return_value=db):
            settler_module.get_pending_phase2()

        assert ("eq", ("chain", "base")) in db.calls

    def test_settlement_queries_select_email_dedupe_fields(self):
        db = _QueryRecorder()

        with patch("src.bots.expiry_settler.get_client", return_value=db):
            settler_module.get_expired_unsettled()
            settler_module.get_pending_phase2()

        selected = " ".join(args[0] for method, args in db.calls if method == "select")
        assert "result_sent_at" in selected
        assert "settled_at" in selected
        assert "net_premium" in selected

    def test_restart_phase2_recovery_does_not_send_legacy_result_email(self):
        recovered = {
            "id": "evt-recovered",
            "user_address": "0x0000000000000000000000000000000000000001",
            "vault_id": 1,
            "otoken_address": "0xtoken",
            "expiry": 1711526400,
            "amount": "100000000",
            "strike_price": "200000000000",
            "is_put": True,
            "mm_address": "0x0000000000000000000000000000000000000002",
            "asset": "eth",
            "is_settled": True,
            "settlement_type": "cash",
            "delivery_tx_hash": None,
            "is_itm": False,
            "settled_at": "2026-05-01T08:00:00+00:00",
            "result_sent_at": None,
        }

        with (
            patch("src.bots.expiry_settler.get_expired_unsettled", return_value=[]),
            patch(
                "src.bots.expiry_settler.get_pending_phase2", return_value=[recovered]
            ),
            patch(
                "src.bots.expiry_settler._preflight_positions",
                side_effect=lambda positions: positions,
            ),
            patch("src.bots.expiry_settler._ensure_expiry_prices_set"),
            patch("src.bots.expiry_settler.get_batch_settler"),
            patch("src.bots.expiry_settler.get_operator_account"),
            patch(
                "src.bots.expiry_settler.identify_itm_positions",
                return_value=([], {}, set()),
            ),
            patch("src.bots.expiry_settler._db_update"),
            patch("src.bots.expiry_settler._send_settlement_emails") as mock_email,
        ):
            asyncio.run(settler_module.settle_once())

        mock_email.assert_called_once_with([], [])


# ---------------------------------------------------------------------------
# _compute_contra_amount
# ---------------------------------------------------------------------------


class TestStrictAssetBoundary:
    def test_position_asset_must_match_live_otoken_underlying(self):
        position = {
            "asset": "eth",
            "otoken_address": "0x0000000000000000000000000000000000000011",
        }
        otoken = MagicMock()
        otoken.functions.underlying.return_value.call.return_value = (
            settler_module.settings.wbtc_address
        )
        with patch("src.bots.expiry_settler.get_otoken", return_value=otoken):
            with pytest.raises(ValueError, match="Stored/on-chain asset mismatch"):
                settler_module._validate_position_asset(position)

    def test_unknown_asset_never_uses_eth_decimals(self):
        with pytest.raises(ValueError, match="Unsupported Base settlement asset"):
            _compute_contra_amount(100_000_000, 250_000_000_000, True, "unknown")

    def test_unknown_asset_skips_itm_without_oracle_read(self):
        position = {
            "user_address": "0xuser",
            "vault_id": 7,
            "expiry": 1777017600,
            "strike_price": "100000000",
            "is_put": True,
            "asset": "unknown",
            "otoken_address": "0xunknown",
        }
        oracle = MagicMock()
        with patch("src.bots.expiry_settler.get_oracle", return_value=oracle):
            itm, prices, skipped = settler_module.identify_itm_positions([position])
        assert itm == []
        assert prices == {}
        assert skipped == {("0xuser", 7)}
        oracle.functions.getExpiryPrice.assert_not_called()


class TestComputeContraAmount:
    def test_put_decimal_scaling(self):
        # 1 oToken (1e8 raw) → 1 WETH (1e18 raw)
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            contra, token_in, token_out = _compute_contra_amount(
                100_000_000, 250_000_000_000, is_put=True, asset="eth"
            )
        assert contra == 100_000_000 * (10**10)  # 1e18
        assert token_in == "0xUSDC"
        assert token_out == "0xWETH"

    def test_call_decimal_scaling(self):
        # 1 oToken at $2500 strike → 2500 USDC (2_500_000 in 6-dec)
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            contra, token_in, token_out = _compute_contra_amount(
                100_000_000, 250_000_000_000, is_put=False, asset="eth"
            )
        assert contra == 2_500_000_000  # 2500 USDC in 6-dec (2500 * 1e6)
        assert token_in == "0xWETH"
        assert token_out == "0xUSDC"

    def test_call_dust_truncation_logs_warning(self, caplog):
        # amount_raw=1, strike=5_000_000_000 ($50) → truncates to 0
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            import logging

            with caplog.at_level(logging.WARNING, logger="src.bots.expiry_settler"):
                contra, _, _ = _compute_contra_amount(
                    1, 5_000_000_000, is_put=False, asset="eth"
                )
        assert contra == 0
        assert "truncated to 0" in caplog.text


# ---------------------------------------------------------------------------
# _beta_compute_max_collateral_put
# ---------------------------------------------------------------------------


class TestBetaComputeMaxCollateralPut:
    def test_normal_case(self):
        # 1 WETH (1e18) at $2500 oracle → ~2500 USDC input + 10% = ~2750
        contra_weth = 1_000_000_000_000_000_000  # 1 WETH
        oracle_price = 250_000_000_000  # $2500 in 8-dec
        result = _beta_compute_max_collateral_put(contra_weth, oracle_price)
        expected_base = (contra_weth * oracle_price) // (10**20)  # 2500 USDC (6-dec)
        expected_max = (
            expected_base + (expected_base * BETA_SLIPPAGE_BPS + 9_999) // 10_000
        )
        assert result == expected_max

    def test_10_percent_buffer_applied(self):
        # buffer must be at least 10% of the base amount
        contra_weth = 1_000_000_000_000_000_000
        oracle_price = 250_000_000_000
        base = (contra_weth * oracle_price) // (10**20)
        result = _beta_compute_max_collateral_put(contra_weth, oracle_price)
        assert result >= base * 11 // 10  # at least 110% of base

    def test_zero_oracle_price_raises(self):
        with pytest.raises(ValueError, match="oracle_price_8dec must be positive"):
            _beta_compute_max_collateral_put(1_000_000_000_000_000_000, 0)

    def test_negative_oracle_price_raises(self):
        with pytest.raises(ValueError, match="oracle_price_8dec must be positive"):
            _beta_compute_max_collateral_put(1_000_000_000_000_000_000, -1)

    def test_ceiling_buffer_formula(self):
        # Verify ceiling: (1 * 1000 + 9999) // 10000 = 1, not 0
        # i.e. even a 1-unit amount gets at least 1 unit of buffer
        result = _beta_compute_max_collateral_put(
            1_000_000_000_000_000_000, 100_000_000
        )
        base = (1_000_000_000_000_000_000 * 100_000_000) // (10**20)
        expected = base + (base * BETA_SLIPPAGE_BPS + 9_999) // 10_000
        assert result == expected


# ---------------------------------------------------------------------------
# _compute_min_amount_out
# ---------------------------------------------------------------------------


class TestComputeMinAmountOut:
    def test_beta_mode_10_percent_buffer(self):
        with patch.object(settler_module.settings, "beta_mode", True):
            result = _compute_min_amount_out(10_000_000)
        assert result == 9_000_000  # 10% off

    def test_production_mode_uses_slippage_tolerance(self):
        with (
            patch.object(settler_module.settings, "beta_mode", False),
            patch.object(settler_module.settings, "swap_slippage_tolerance", 0.01),
        ):
            result = _compute_min_amount_out(10_000_000)
        assert result == 9_900_000  # 1% off

    def test_zero_contra_amount_raises(self):
        with pytest.raises(ValueError, match="contra_amount must be positive"):
            _compute_min_amount_out(0)

    def test_negative_contra_amount_raises(self):
        with pytest.raises(ValueError, match="contra_amount must be positive"):
            _compute_min_amount_out(-1)

    def test_beta_bps_constant_used(self):
        # Confirm the module-level constant drives the calculation
        with patch.object(settler_module.settings, "beta_mode", True):
            result = _compute_min_amount_out(10_000)
        expected = 10_000 - (10_000 * BETA_SLIPPAGE_BPS) // 10_000
        assert result == expected


# ---------------------------------------------------------------------------
# compute_slippage_param — dispatch and routing
# ---------------------------------------------------------------------------


class TestComputeSlippageParam:
    def test_call_routes_to_min_amount_out(self):
        # CALL: must not touch Quoter; returns (minAmountOut, contra_amount)
        pos = _call_position()
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch.object(settler_module.settings, "beta_mode", True),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler.get_uniswap_quoter") as mock_quoter,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            slippage, contra = compute_slippage_param(pos)
        mock_quoter.assert_not_called()
        assert contra == 2_500_000_000  # 1 oToken × $2500 strike in 6-dec (2500 * 1e6)
        assert slippage == contra - (contra * BETA_SLIPPAGE_BPS) // 10_000

    def test_put_beta_mode_routes_to_oracle_path(self):
        # PUT beta: must not touch Quoter; uses oracle price
        pos = _put_position()
        oracle_price = 250_000_000_000
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch.object(settler_module.settings, "beta_mode", True),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler.get_uniswap_quoter") as mock_quoter,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            slippage, contra = compute_slippage_param(
                pos, oracle_price_8dec=oracle_price
            )
        mock_quoter.assert_not_called()
        assert contra == 100_000_000 * (10**10)  # 1 WETH

    def test_put_beta_mode_raises_without_oracle_price(self):
        pos = _put_position()
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch.object(settler_module.settings, "beta_mode", True),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            with pytest.raises(ValueError, match="oracle_price_8dec is required"):
                compute_slippage_param(pos, oracle_price_8dec=None)

    def test_put_production_mode_uses_quoter(self):
        pos = _put_position()
        mock_quoter_instance = MagicMock()
        mock_quoter_instance.functions.quoteExactOutputSingle.return_value.call.return_value = [
            2_400_000_000_000_000_000,  # amount_in: ~0.96 WETH
            0,
            0,
            0,
        ]
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch.object(settler_module.settings, "beta_mode", False),
            patch.object(settler_module.settings, "swap_slippage_tolerance", 0.01),
            patch.object(settler_module.settings, "uniswap_fee_tier", 3000),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch(
                "src.bots.expiry_settler.get_uniswap_quoter",
                return_value=mock_quoter_instance,
            ),
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            slippage, contra = compute_slippage_param(pos)
        amount_in = 2_400_000_000_000_000_000
        expected = amount_in + (amount_in * 100 + 9_999) // 10_000  # 1% slippage
        assert slippage == expected
        assert contra == 100_000_000 * (10**10)

    def test_zero_amount_raw_raises(self):
        pos = _call_position(amount_raw=0)
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            with pytest.raises(ValueError, match="amount_raw must be positive"):
                compute_slippage_param(pos)

    def test_zero_strike_raises(self):
        pos = _call_position(strike=0)
        with (
            patch.object(settler_module.settings, "weth_address", "0xWETH"),
            patch.object(settler_module.settings, "usdc_address", "0xUSDC"),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            with pytest.raises(ValueError, match="strike_price must be positive"):
                compute_slippage_param(pos)


# ---------------------------------------------------------------------------
# _physical_redeem_with_retry
# ---------------------------------------------------------------------------


def _itm_position() -> dict:
    """Build a minimal ITM position for retry tests."""
    return {
        "otoken_address": "0xOTOKEN",
        "user_address": "0xUSER",
        "mm_address": "0xMM",
        "amount": "100000000",
        "strike_price": "250000000000",
        "is_put": True,
        "vault_id": 1,
        "asset": "eth",
        "expiry_price_raw": 240000000000,
    }


class TestPhysicalRedeemWithRetry:
    def test_succeeds_first_attempt(self):
        """Happy path: first attempt succeeds, no retry needed."""
        pos = _itm_position()
        mock_settler = MagicMock()
        mock_account = MagicMock()

        async def run():
            return await _physical_redeem_with_retry(
                pos,
                mock_settler,
                mock_account,
                240000000000,
            )

        with (
            patch.object(settler_module.settings, "settlement_max_retries", 3),
            patch(
                "src.bots.expiry_settler.compute_slippage_param",
                return_value=(1000, 500),
            ),
            patch(
                "src.bots.expiry_settler.build_and_send_tx",
                return_value="0xTXHASH",
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            tx_hash, contra = asyncio.run(run())

        assert tx_hash == "0xTXHASH"
        assert contra == 500

    def test_route_change_requotes_before_single_broadcast(self):
        pos = {**_itm_position(), "asset": "nvdac"}
        first_quote = MagicMock(
            slippage_param=1000,
            contra_amount=500,
            fingerprint=("0xold", "0x0", 0),
        )
        second_quote = MagicMock(
            slippage_param=1100,
            contra_amount=500,
            fingerprint=("0xnew", "0x0", 0),
        )

        with (
            patch.object(settler_module.settings, "settlement_max_retries", 2),
            patch(
                "src.bots.expiry_settler._compute_contra_amount",
                return_value=(500, "0xin", "0xout"),
            ),
            patch(
                "src.bots.expiry_settler.build_route_quote",
                side_effect=[first_quote, second_quote],
            ) as quote,
            patch(
                "src.bots.expiry_settler.assert_route_unchanged",
                side_effect=[ValueError("route changed"), None],
            ) as unchanged,
            patch(
                "src.bots.expiry_settler.build_and_send_tx",
                return_value="0xTXHASH",
            ) as send,
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler.asyncio.sleep", return_value=None),
        ):
            mock_web3.to_checksum_address.side_effect = lambda value: value
            result = asyncio.run(
                _physical_redeem_with_retry(pos, MagicMock(), MagicMock(), None)
            )

        assert result == ("0xTXHASH", 500)
        assert quote.call_count == 2
        assert [call.args[1] for call in unchanged.call_args_list] == [
            first_quote.fingerprint,
            second_quote.fingerprint,
        ]
        send.assert_called_once()

    def test_succeeds_on_second_attempt(self):
        """First attempt fails, second succeeds."""
        pos = _itm_position()
        mock_settler = MagicMock()
        mock_account = MagicMock()

        call_count = 0

        def build_tx_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("flash loan reverted")
            return "0xTXHASH_RETRY"

        async def run():
            return await _physical_redeem_with_retry(
                pos,
                mock_settler,
                mock_account,
                240000000000,
            )

        with (
            patch.object(settler_module.settings, "settlement_max_retries", 3),
            patch(
                "src.bots.expiry_settler.compute_slippage_param",
                return_value=(1000, 500),
            ),
            patch(
                "src.bots.expiry_settler.build_and_send_tx",
                side_effect=build_tx_side_effect,
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch(
                "src.bots.expiry_settler.asyncio.sleep", return_value=None
            ) as mock_sleep,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            tx_hash, contra = asyncio.run(run())

        assert tx_hash == "0xTXHASH_RETRY"
        mock_sleep.assert_called_once_with(60)

    def test_all_retries_exhausted_raises(self):
        """All retries fail → raises exception with ALERT log."""
        pos = _itm_position()
        mock_settler = MagicMock()
        mock_account = MagicMock()

        async def run():
            return await _physical_redeem_with_retry(
                pos,
                mock_settler,
                mock_account,
                240000000000,
            )

        with (
            patch.object(settler_module.settings, "settlement_max_retries", 2),
            patch(
                "src.bots.expiry_settler.compute_slippage_param",
                side_effect=RuntimeError("quoter down"),
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler.asyncio.sleep", return_value=None),
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            with pytest.raises(RuntimeError, match="quoter down"):
                asyncio.run(run())

    def test_backoff_escalates(self):
        """Backoff delays increase with each attempt."""
        pos = _itm_position()
        mock_settler = MagicMock()
        mock_account = MagicMock()

        async def run():
            return await _physical_redeem_with_retry(
                pos,
                mock_settler,
                mock_account,
                None,
            )

        with (
            patch.object(settler_module.settings, "settlement_max_retries", 4),
            patch(
                "src.bots.expiry_settler.compute_slippage_param",
                side_effect=RuntimeError("fail"),
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch(
                "src.bots.expiry_settler.asyncio.sleep", return_value=None
            ) as mock_sleep,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            with pytest.raises(RuntimeError):
                asyncio.run(run())

        # 3 sleeps (attempts 1-3 retry, attempt 4 raises)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        assert delays == [60, 300, 900]

    def test_zero_max_retries_raises_immediately(self):
        """max_retries=0 raises ValueError, not a silent None return."""
        pos = _itm_position()

        with patch.object(settler_module.settings, "settlement_max_retries", 0):
            with pytest.raises(ValueError, match="settlement_max_retries must be >= 1"):
                asyncio.run(
                    _physical_redeem_with_retry(pos, MagicMock(), MagicMock(), None)
                )

    def test_zero_slippage_param_is_retried(self):
        """slippage_param <= 0 triggers retry (ValueError inside loop)."""
        pos = _itm_position()
        mock_settler = MagicMock()

        with (
            patch.object(settler_module.settings, "settlement_max_retries", 2),
            patch(
                "src.bots.expiry_settler.compute_slippage_param",
                return_value=(0, 500),
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler.asyncio.sleep", return_value=None),
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            with pytest.raises(ValueError, match="slippage_param=0"):
                asyncio.run(
                    _physical_redeem_with_retry(
                        pos,
                        mock_settler,
                        MagicMock(),
                        None,
                    )
                )


# ---------------------------------------------------------------------------
# _post_settle_sweep
# ---------------------------------------------------------------------------


class TestPostSettleSweep:
    def test_exits_when_no_unsettled(self):
        """Sweep exits immediately when no unsettled positions."""
        with (
            patch.object(
                settler_module.settings, "settlement_sweep_interval_seconds", 1
            ),
            patch.object(settler_module.settings, "settlement_sweep_max_cycles", 5),
            patch(
                "src.bots.expiry_settler.get_expired_unsettled",
                return_value=[],
            ),
            patch("src.bots.expiry_settler.settle_once") as mock_settle,
            patch("src.bots.expiry_settler.asyncio.sleep", return_value=None),
        ):
            asyncio.run(_post_settle_sweep())

        mock_settle.assert_not_called()

    def test_exits_after_max_cycles(self):
        """Sweep stops after max_cycles even if positions remain."""
        with (
            patch.object(
                settler_module.settings, "settlement_sweep_interval_seconds", 1
            ),
            patch.object(settler_module.settings, "settlement_sweep_max_cycles", 3),
            patch(
                "src.bots.expiry_settler.get_expired_unsettled",
                return_value=[{"id": 1}],
            ),
            patch("src.bots.expiry_settler.settle_once") as mock_settle,
            patch("src.bots.expiry_settler.asyncio.sleep", return_value=None),
        ):
            asyncio.run(_post_settle_sweep())

        assert mock_settle.call_count == 3

    def test_stops_when_positions_cleared(self):
        """Sweep stops mid-cycle when all positions are settled."""
        call_count = 0

        def unsettled_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return [{"id": 1}]
            return []

        with (
            patch.object(
                settler_module.settings, "settlement_sweep_interval_seconds", 1
            ),
            patch.object(settler_module.settings, "settlement_sweep_max_cycles", 10),
            patch(
                "src.bots.expiry_settler.get_expired_unsettled",
                side_effect=unsettled_side_effect,
            ),
            patch("src.bots.expiry_settler.settle_once") as mock_settle,
            patch("src.bots.expiry_settler.asyncio.sleep", return_value=None),
        ):
            asyncio.run(_post_settle_sweep())

        assert mock_settle.call_count == 1

    def test_continues_after_settle_once_failure(self):
        """If settle_once() raises, sweep continues to next cycle."""
        call_count = 0

        def unsettled_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return [{"id": 1}]
            return []

        async def settle_raise():
            raise RuntimeError("on-chain failure")

        with (
            patch.object(
                settler_module.settings, "settlement_sweep_interval_seconds", 1
            ),
            patch.object(settler_module.settings, "settlement_sweep_max_cycles", 5),
            patch(
                "src.bots.expiry_settler.get_expired_unsettled",
                side_effect=unsettled_side_effect,
            ),
            patch(
                "src.bots.expiry_settler.settle_once",
                side_effect=settle_raise,
            ) as mock_settle,
            patch("src.bots.expiry_settler.asyncio.sleep", return_value=None),
        ):
            asyncio.run(_post_settle_sweep())

        # 2 cycles with positions, settle_once called both times despite failure
        assert mock_settle.call_count == 2


# ---------------------------------------------------------------------------
# _reconcile_settled_on_chain
# ---------------------------------------------------------------------------


def _unsettled_position(user="0xuser", vault_id=1):
    return {"user_address": user, "vault_id": vault_id}


class TestReconcileSettledOnChain:
    def test_settled_on_chain_returned_in_already_settled_bucket(self):
        """Position settled on-chain is moved out of the unsettled list AND
        returned in the second tuple entry so the caller can still feed it
        into Phase 2 (physical delivery)."""
        pos = _unsettled_position()
        mock_controller = MagicMock()
        mock_controller.functions.vaultSettled.return_value.call.return_value = True

        with (
            patch(
                "src.bots.expiry_settler.get_controller", return_value=mock_controller
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler._db_update") as mock_db,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            unsettled, already_settled = _reconcile_settled_on_chain([pos])

        assert unsettled == []
        assert already_settled == [pos]
        mock_db.assert_called_once()
        call_fields = mock_db.call_args[0][2]
        assert call_fields["is_settled"] is True

    def test_unsettled_on_chain_kept_in_list(self):
        """Position not settled on-chain stays in the unsettled list."""
        pos = _unsettled_position()
        mock_controller = MagicMock()
        mock_controller.functions.vaultSettled.return_value.call.return_value = False

        with (
            patch(
                "src.bots.expiry_settler.get_controller", return_value=mock_controller
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler._db_update") as mock_db,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            unsettled, already_settled = _reconcile_settled_on_chain([pos])

        assert unsettled == [pos]
        assert already_settled == []
        mock_db.assert_not_called()

    def test_rpc_failure_assumes_unsettled(self):
        """If vaultSettled call fails, position stays in unsettled list."""
        pos = _unsettled_position()
        mock_controller = MagicMock()
        mock_controller.functions.vaultSettled.return_value.call.side_effect = (
            RuntimeError("RPC down")
        )

        with (
            patch(
                "src.bots.expiry_settler.get_controller", return_value=mock_controller
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            unsettled, already_settled = _reconcile_settled_on_chain([pos])

        assert unsettled == [pos]
        assert already_settled == []

    def test_mixed_positions(self):
        """Mix of settled and unsettled — split correctly into both buckets."""
        settled_pos = _unsettled_position("0xsettled", 1)
        unsettled_pos = _unsettled_position("0xunsettled", 2)
        mock_controller = MagicMock()

        def vault_settled_side_effect(owner, vault_id):
            mock_call = MagicMock()
            mock_call.call.return_value = owner == "0xsettled"
            return mock_call

        mock_controller.functions.vaultSettled = vault_settled_side_effect

        with (
            patch(
                "src.bots.expiry_settler.get_controller", return_value=mock_controller
            ),
            patch("src.bots.expiry_settler.Web3") as mock_web3,
            patch("src.bots.expiry_settler._db_update"),
        ):
            mock_web3.to_checksum_address.side_effect = lambda x: x
            unsettled, already_settled = _reconcile_settled_on_chain(
                [settled_pos, unsettled_pos]
            )

        assert len(unsettled) == 1
        assert unsettled[0]["user_address"] == "0xunsettled"
        assert len(already_settled) == 1
        assert already_settled[0]["user_address"] == "0xsettled"


class TestEnsureExpiryPricesSetIsPositionScoped:
    def test_reads_only_requested_asset_expiry_pairs(self):
        seen_assets = []

        def fake_price_raw(asset):
            seen_assets.append(asset.value)
            return 230000000000, 8, 1777017600

        mock_oracle = MagicMock()
        mock_oracle.functions.getExpiryPrice.return_value.call.return_value = (0, False)

        with (
            patch("src.bots.expiry_settler.get_oracle", return_value=mock_oracle),
            patch("src.bots.expiry_settler.get_operator_account"),
            patch(
                "src.bots.expiry_settler.get_asset_price_raw",
                side_effect=fake_price_raw,
            ),
            patch(
                "src.bots.expiry_settler.build_and_send_tx", return_value="0xdeadbeef"
            ),
        ):
            _ensure_expiry_prices_set({("eth", 1777017600)})

        assert seen_assets == ["eth"]
        mock_oracle.functions.setExpiryPrice.assert_called_once()
