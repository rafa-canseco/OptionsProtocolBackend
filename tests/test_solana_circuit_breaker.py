"""Tests for Solana circuit breaker bot and Base bot chain filter fix."""

import hashlib

import pytest
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from unittest.mock import MagicMock, patch


class TestBaseCircuitBreakerChainFilter:
    """Verify Base bot only deactivates chain='base' quotes."""

    @pytest.mark.asyncio
    @patch("src.bots.circuit_breaker_bot.get_client")
    @patch("src.bots.circuit_breaker_bot.build_and_send_tx")
    @patch("src.bots.circuit_breaker_bot.get_operator_account")
    @patch("src.bots.circuit_breaker_bot.get_batch_settler")
    async def test_invalidate_filters_by_base_chain(
        self,
        mock_settler,
        mock_account,
        mock_send_tx,
        mock_db,
    ):
        mock_send_tx.return_value = "0xfaketx"
        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        # Build the mock chain: .update().eq("is_active", True).eq("chain", "base").execute()
        chain_eq = MagicMock()
        chain_eq.execute.return_value = MagicMock(data=[{}])
        active_eq = MagicMock()
        active_eq.eq.return_value = chain_eq
        mock_table.update.return_value.eq.return_value = active_eq

        from src.bots.circuit_breaker_bot import invalidate_quotes

        await invalidate_quotes("eth")

        # Verify the filter chain was called correctly
        mock_table.update.assert_called_once_with({"is_active": False})
        mock_table.update.return_value.eq.assert_called_with("is_active", True)
        active_eq.eq.assert_called_with("chain", "base")


class TestBuildIncrementNonceInstruction:
    """Verify the Anchor instruction is built correctly."""

    def test_discriminator_matches_anchor(self):
        from src.bots.solana_circuit_breaker_bot import _NONCE_DISCRIMINATOR

        expected = hashlib.sha256(b"global:increment_maker_nonce").digest()[:8]
        assert _NONCE_DISCRIMINATOR == expected

    @patch("src.bots.solana_circuit_breaker_bot.settings")
    def test_instruction_has_correct_structure(self, mock_settings):
        mock_settings.solana_batch_settler_program_id = str(Pubkey.new_unique())
        from src.bots.solana_circuit_breaker_bot import build_increment_nonce_ix

        operator = Keypair()
        ix = build_increment_nonce_ix(operator.pubkey())

        assert len(ix.accounts) == 2
        # maker_state: writable, not signer
        assert ix.accounts[0].is_writable is True
        assert ix.accounts[0].is_signer is False
        # operator: signer, not writable
        assert ix.accounts[1].pubkey == operator.pubkey()
        assert ix.accounts[1].is_signer is True
        assert ix.accounts[1].is_writable is False
        # data is just the 8-byte discriminator
        assert len(ix.data) == 8


class TestSolanaInvalidateQuotes:
    """Verify the full invalidation flow."""

    @pytest.mark.asyncio
    @patch("src.bots.solana_circuit_breaker_bot.VersionedTransaction")
    @patch("src.bots.solana_circuit_breaker_bot.MessageV0")
    @patch("src.bots.solana_circuit_breaker_bot.get_client")
    @patch("src.bots.solana_circuit_breaker_bot.build_and_send_solana_tx")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_client")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_operator")
    @patch("src.bots.solana_circuit_breaker_bot.settings")
    async def test_invalidate_sends_tx_and_deactivates_db(
        self,
        mock_settings,
        mock_get_operator,
        mock_get_client,
        mock_send_tx,
        mock_db,
        mock_msg,
        mock_vtx,
    ):
        kp = Keypair()
        mock_get_operator.return_value = kp
        mock_settings.solana_batch_settler_program_id = str(Pubkey.new_unique())
        mock_send_tx.return_value = "fakesig123"
        mock_rpc = MagicMock()
        mock_rpc.get_latest_blockhash.return_value = MagicMock(
            value=MagicMock(blockhash=MagicMock())
        )
        mock_get_client.return_value = mock_rpc

        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        chain_eq = MagicMock()
        chain_eq.execute.return_value = MagicMock(data=[{}, {}])
        active_eq = MagicMock()
        active_eq.eq.return_value = chain_eq
        mock_table.update.return_value.eq.return_value = active_eq

        from src.bots.solana_circuit_breaker_bot import invalidate_solana_quotes

        await invalidate_solana_quotes("sol")

        mock_send_tx.assert_called_once()
        mock_table.update.assert_called_once_with({"is_active": False})

    @pytest.mark.asyncio
    @patch("src.bots.solana_circuit_breaker_bot.VersionedTransaction")
    @patch("src.bots.solana_circuit_breaker_bot.MessageV0")
    @patch("src.bots.solana_circuit_breaker_bot.get_client")
    @patch("src.bots.solana_circuit_breaker_bot.build_and_send_solana_tx")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_client")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_operator")
    @patch("src.bots.solana_circuit_breaker_bot.settings")
    async def test_invalidate_filters_by_solana_chain(
        self,
        mock_settings,
        mock_get_operator,
        mock_get_client,
        mock_send_tx,
        mock_db,
        mock_msg,
        mock_vtx,
    ):
        kp = Keypair()
        mock_get_operator.return_value = kp
        mock_settings.solana_batch_settler_program_id = str(Pubkey.new_unique())
        mock_send_tx.return_value = "fakesig"
        mock_rpc = MagicMock()
        mock_rpc.get_latest_blockhash.return_value = MagicMock(
            value=MagicMock(blockhash=MagicMock())
        )
        mock_get_client.return_value = mock_rpc

        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        chain_eq = MagicMock()
        chain_eq.execute.return_value = MagicMock(data=[])
        active_eq = MagicMock()
        active_eq.eq.return_value = chain_eq
        mock_table.update.return_value.eq.return_value = active_eq

        from src.bots.solana_circuit_breaker_bot import invalidate_solana_quotes

        await invalidate_solana_quotes("sol")

        # Verify filter chain: .eq("is_active", True) then .eq("chain", "solana")
        mock_table.update.return_value.eq.assert_called_with("is_active", True)
        active_eq.eq.assert_called_with("chain", "solana")


class TestCheckOnce:
    """Verify the main orchestration loop."""

    @pytest.mark.asyncio
    @patch("src.bots.solana_circuit_breaker_bot.invalidate_solana_quotes")
    @patch("src.bots.solana_circuit_breaker_bot.circuit_breaker")
    @patch("src.bots.solana_circuit_breaker_bot.get_pyth_price")
    async def test_calls_invalidate_on_trip(self, mock_pyth, mock_cb, mock_invalidate):
        mock_pyth.return_value = (150.0, 1700000000)
        mock_cb.check.return_value = True
        mock_cb.pause_reason_for.return_value = "SOL moved 3%"
        mock_invalidate.return_value = None

        from src.bots.solana_circuit_breaker_bot import check_once

        await check_once()

        mock_invalidate.assert_called_once_with("sol")
        mock_cb.update_reference.assert_called_once_with(150.0, "sol")

    @pytest.mark.asyncio
    @patch("src.bots.solana_circuit_breaker_bot.invalidate_solana_quotes")
    @patch("src.bots.solana_circuit_breaker_bot.circuit_breaker")
    @patch("src.bots.solana_circuit_breaker_bot.get_pyth_price")
    async def test_skips_when_no_trip(self, mock_pyth, mock_cb, mock_invalidate):
        mock_pyth.return_value = (150.0, 1700000000)
        mock_cb.check.return_value = False

        from src.bots.solana_circuit_breaker_bot import check_once

        await check_once()

        mock_invalidate.assert_not_called()
        mock_cb.update_reference.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.bots.solana_circuit_breaker_bot.invalidate_solana_quotes")
    @patch("src.bots.solana_circuit_breaker_bot.circuit_breaker")
    @patch("src.bots.solana_circuit_breaker_bot.get_pyth_price")
    async def test_skips_on_pyth_failure(self, mock_pyth, mock_cb, mock_invalidate):
        mock_pyth.side_effect = RuntimeError("Pyth down")

        from src.bots.solana_circuit_breaker_bot import check_once

        await check_once()

        mock_cb.check.assert_not_called()
        mock_invalidate.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.bots.solana_circuit_breaker_bot.invalidate_solana_quotes")
    @patch("src.bots.solana_circuit_breaker_bot.circuit_breaker")
    @patch("src.bots.solana_circuit_breaker_bot.get_pyth_price")
    async def test_does_not_reset_when_invalidate_fails(
        self, mock_pyth, mock_cb, mock_invalidate
    ):
        mock_pyth.return_value = (150.0, 1700000000)
        mock_cb.check.return_value = True
        mock_cb.pause_reason_for.return_value = "SOL moved 3%"
        mock_invalidate.side_effect = RuntimeError("tx failed")

        from src.bots.solana_circuit_breaker_bot import check_once

        with pytest.raises(RuntimeError, match="tx failed"):
            await check_once()

        mock_cb.update_reference.assert_not_called()
