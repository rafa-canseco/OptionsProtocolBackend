"""Tests for Solana circuit breaker bot and Base bot chain filter fix."""

import pytest
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
