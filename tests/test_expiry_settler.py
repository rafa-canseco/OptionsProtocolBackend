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
    _SETTLE_FIELDS,
    _beta_compute_max_collateral_put,
    _compute_contra_amount,
    _compute_min_amount_out,
    _ensure_expiry_prices_set,
    _physical_redeem_with_retry,
    _post_settle_sweep,
    _reconcile_settled_on_chain,
    compute_slippage_param,
    get_expired_unsettled,
    get_pending_phase2,
    settle_once,
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
    }


def _put_position(amount_raw: int = 100_000_000, strike: int = 250_000_000_000) -> dict:
    """Return a minimal PUT position dict (1 oToken at $2500 strike)."""
    return {
        "amount": str(amount_raw),
        "strike_price": str(strike),
        "is_put": True,
        "otoken_address": "0xPUT",
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
            patch("src.bots.expiry_settler.get_pending_phase2", return_value=[recovered]),
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
                100_000_000, 250_000_000_000, is_put=True
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
                100_000_000, 250_000_000_000, is_put=False
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
                contra, _, _ = _compute_contra_amount(1, 5_000_000_000, is_put=False)
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


class TestEnsureExpiryPricesSetSkipsNonEvmAssets:
    """Solana assets must be skipped: the EVM Oracle has no price feed for
    them and Web3.to_checksum_address rejects their base58 mint addresses."""

    def test_skips_solana_assets_without_calling_to_checksum(self):
        from src.chains import Chain
        from src.pricing.assets import Asset

        seen_assets: list[Asset] = []

        def fake_price_raw(asset):
            seen_assets.append(asset)
            return 230000000000, 8, 1777017500

        mock_oracle = MagicMock()
        finalized_call = MagicMock()
        finalized_call.call.return_value = (0, False)
        mock_oracle.functions.getExpiryPrice.return_value = finalized_call
        set_call = MagicMock()
        mock_oracle.functions.setExpiryPrice.return_value = set_call

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
            _ensure_expiry_prices_set({1777017600})

        assert seen_assets, "expected at least one EVM asset iteration"
        for a in seen_assets:
            from src.pricing.assets import get_asset_config

            assert get_asset_config(a).chain == Chain.BASE


# ---------------------------------------------------------------------------
# Phase 2 recovery: get_pending_phase2 + settle_once orchestration
# ---------------------------------------------------------------------------


class _StubQuery:
    """Records the chained PostgREST builder calls so tests can assert
    on the exact filter used. Returns canned `data` on `.execute()`."""

    def __init__(self, response_data=None):
        self.calls: list[tuple[str, tuple]] = []
        self._response = response_data or []

    @property
    def not_(self):
        # PostgREST exposes `not_` as a property, not a callable: the
        # chain is `.not_.is_(col, val)`. Returning self keeps the
        # subsequent method recordable on the same stub.
        self.calls.append(("not_", ()))
        return self

    def __getattr__(self, name):
        def _record(*args):
            self.calls.append((name, args))
            return self

        return _record

    def execute(self):
        self.calls.append(("execute", ()))
        return MagicMock(data=self._response)


class TestSettleFieldsProjection:
    def test_get_expired_unsettled_projection_includes_phase2_fields(self):
        """The projection must carry settlement_type, delivery_tx_hash and
        is_itm so Phase 2 recovery can decide whether a row needs delivery
        without a follow-up read. A future refactor that drops one would
        silently break recovery filtering — pin them here."""
        for f in ("settlement_type", "delivery_tx_hash", "is_itm"):
            assert f in _SETTLE_FIELDS

    def test_get_expired_unsettled_uses_settle_fields_constant(self):
        stub = _StubQuery([])
        client = MagicMock()
        client.table.return_value = stub
        with patch("src.bots.expiry_settler.get_client", return_value=client):
            get_expired_unsettled()
        select_calls = [c for c in stub.calls if c[0] == "select"]
        assert len(select_calls) == 1
        assert select_calls[0][1][0] == _SETTLE_FIELDS


class TestGetPendingPhase2:
    def _run(self, response):
        stub = _StubQuery(response)
        client = MagicMock()
        client.table.return_value = stub
        with patch("src.bots.expiry_settler.get_client", return_value=client):
            result = get_pending_phase2()
        return result, stub

    def test_filter_excludes_unsettled_rows(self):
        """Phase 2 recovery only targets is_settled=True rows. Pulling
        is_settled=False would re-run Phase 1 wastefully and contradict
        get_expired_unsettled's contract."""
        _, stub = self._run([])
        eq_calls = [args for op, args in stub.calls if op == "eq"]
        assert ("is_settled", True) in eq_calls

    def test_filter_excludes_already_delivered(self):
        """Rows with a delivery_tx_hash MUST be skipped — running Phase 2
        again would emit a second physicalRedeem and double-charge the MM."""
        _, stub = self._run([])
        is_calls = [args for op, args in stub.calls if op == "is_"]
        assert ("delivery_tx_hash", "null") in is_calls

    def test_filter_excludes_physical_failed(self):
        """physical_failed rows have exhausted retries; recovery would
        loop forever. Operator must intervene manually."""
        _, stub = self._run([])
        or_args = [args[0] for op, args in stub.calls if op == "or_"]
        joined = " | ".join(or_args)
        assert "settlement_type.is.null" in joined
        assert "settlement_type.eq.cash" in joined
        assert "physical_failed" not in joined

    def test_filter_includes_unknown_or_itm_only(self):
        """is_itm=False (known OTM) rows do not need Phase 2; exclude them."""
        _, stub = self._run([])
        or_args = [args[0] for op, args in stub.calls if op == "or_"]
        joined = " | ".join(or_args)
        assert "is_itm.is.null" in joined
        assert "is_itm.eq.true" in joined

    def test_returns_rows_when_present(self):
        rows = [{"vault_id": 1, "user_address": "0xu", "expiry": 100}]
        result, _ = self._run(rows)
        assert result == rows

    def test_empty_when_no_rows(self):
        result, _ = self._run([])
        assert result == []


def _phase2_recovery_position(user="0xu", vault_id=1, expiry=1_700_000_000):
    return {
        "id": f"id-{vault_id}",
        "user_address": user,
        "vault_id": vault_id,
        "otoken_address": "0xtoken",
        "expiry": expiry,
        "amount": "100000000",
        "strike_price": "230000000000",
        "is_put": False,
        "mm_address": "0xmm",
        "asset": "eth",
        "is_settled": True,
        "settlement_type": "cash",
        "delivery_tx_hash": None,
        "is_itm": None,
    }


class TestSettleOncePhase2Recovery:
    """Cover the scenario the PR exists for: nothing fresh to settle, but
    prior cycle left ITM positions stuck without delivery. settle_once
    must skip Phase 0/1, run identify_itm + delivery loop, and not sleep
    the inter-phase wait when there are no fresh Phase 1 settlements.
    """

    def _run(self, *, expired=None, pending=None, itm_for_phase2=None):
        expired = expired or []
        pending = pending or []
        # identify_itm_positions returns (itm_positions, expiry_cache, skipped_keys)
        itm_for_phase2 = itm_for_phase2 or []
        ensure_called = MagicMock()
        sleep_called = MagicMock()

        async def fake_sleep(seconds):
            sleep_called(seconds)

        async def to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with (
            patch(
                "src.bots.expiry_settler.get_expired_unsettled",
                return_value=expired,
            ),
            patch(
                "src.bots.expiry_settler.get_pending_phase2",
                return_value=pending,
            ),
            patch(
                "src.bots.expiry_settler._reconcile_settled_on_chain",
                return_value=([], []),
            ),
            patch(
                "src.bots.expiry_settler._ensure_expiry_prices_set",
                ensure_called,
            ),
            patch(
                "src.bots.expiry_settler.identify_itm_positions",
                return_value=(itm_for_phase2, {}, set()),
            ),
            patch(
                "src.bots.expiry_settler._physical_redeem_with_retry",
                new=MagicMock(return_value=("0xtx", 1_000_000)),
            ),
            patch("src.bots.expiry_settler._db_update"),
            patch(
                "src.bots.expiry_settler.get_batch_settler", return_value=MagicMock()
            ),
            patch(
                "src.bots.expiry_settler.get_operator_account",
                return_value=MagicMock(),
            ),
            patch("src.bots.expiry_settler.asyncio.sleep", new=fake_sleep),
            patch(
                "src.bots.expiry_settler.asyncio.to_thread",
                new=to_thread,
            ),
            patch("src.bots.expiry_settler._send_settlement_emails"),
            patch("src.bots.expiry_settler._post_settle_sweep"),
        ):
            asyncio.run(settle_once())
        return ensure_called, sleep_called

    def test_phase2_only_path_runs_oracle_and_skips_phase1(self):
        """positions=[] but pending non-empty: settle_once must still
        call _ensure_expiry_prices_set for the pending expiries (Phase 0
        is needed for identify_itm) and proceed past the early-return."""
        pending = [_phase2_recovery_position(expiry=1_777_622_400)]
        ensure_called, _ = self._run(pending=pending)
        ensure_called.assert_called_once()
        expiries_arg = ensure_called.call_args[0][0]
        assert 1_777_622_400 in expiries_arg

    def test_phase2_only_path_skips_inter_phase_wait(self):
        """When no fresh Phase 1 settlements happened this cycle, the
        flash_loan_redeem_delay sleep is wasted — must be skipped."""
        pending = [_phase2_recovery_position()]
        _, sleep_called = self._run(pending=pending)
        # Any real-world delay would be > 1s; confirm we never slept that long.
        slept_seconds = [c[0][0] for c in sleep_called.call_args_list]
        assert all(s == 0 or s is None or s < 1 for s in slept_seconds), (
            f"unexpected long sleep in Phase 2 recovery path: {slept_seconds}"
        )

    def test_dedup_phase2_recovery_against_reconciled_already_settled(self):
        """If a vault appears in BOTH the reconcile-discovered and the
        get_pending_phase2 buckets, the seen_keys dedup must avoid running
        physicalRedeem twice for the same vault."""
        pos = _phase2_recovery_position(vault_id=42)
        seen_phase2: list[dict] = []

        def fake_identify(positions):
            # Capture what settle_once passed to identify_itm_positions
            seen_phase2.extend(positions)
            return ([], {}, set())

        async def fake_sleep(seconds):
            pass

        async def to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with (
            patch("src.bots.expiry_settler.get_expired_unsettled", return_value=[]),
            patch("src.bots.expiry_settler.get_pending_phase2", return_value=[pos]),
            patch(
                "src.bots.expiry_settler._reconcile_settled_on_chain",
                return_value=([], [pos]),  # SAME vault in already_settled
            ),
            patch("src.bots.expiry_settler._ensure_expiry_prices_set"),
            patch(
                "src.bots.expiry_settler.identify_itm_positions",
                side_effect=fake_identify,
            ),
            patch("src.bots.expiry_settler.asyncio.sleep", new=fake_sleep),
            patch("src.bots.expiry_settler.asyncio.to_thread", new=to_thread),
            patch(
                "src.bots.expiry_settler.get_batch_settler", return_value=MagicMock()
            ),
            patch(
                "src.bots.expiry_settler.get_operator_account",
                return_value=MagicMock(),
            ),
            patch("src.bots.expiry_settler._send_settlement_emails"),
            patch("src.bots.expiry_settler._post_settle_sweep"),
        ):
            # Need both buckets non-empty so settle_once doesn't early-return
            asyncio.run(settle_once())

        # The same vault must appear exactly once in the Phase 2 input,
        # never twice — guards against double physicalRedeem.
        keys = [(p["user_address"], p["vault_id"]) for p in seen_phase2]
        assert keys.count((pos["user_address"], pos["vault_id"])) == 1
