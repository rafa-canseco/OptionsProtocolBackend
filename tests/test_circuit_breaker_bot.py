import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.bots import circuit_breaker_bot


def _mock_quotes_table(rows):
    table = MagicMock()
    table.select.return_value.eq.return_value.gt.return_value.limit.return_value.execute.return_value.data = rows
    return table


def test_has_active_non_expired_quotes_false_when_empty():
    table = _mock_quotes_table([])
    with patch("src.bots.circuit_breaker_bot.get_client") as mock_db:
        mock_db.return_value.table.return_value = table
        assert circuit_breaker_bot._has_active_non_expired_quotes() is False


def test_has_active_non_expired_quotes_true_when_row_present():
    table = _mock_quotes_table([{"id": "quote-1"}])
    with patch("src.bots.circuit_breaker_bot.get_client") as mock_db:
        mock_db.return_value.table.return_value = table
        assert circuit_breaker_bot._has_active_non_expired_quotes() is True


def test_has_active_non_expired_quotes_raises_on_data_none():
    """Silent Supabase failure (data=None) must raise, not masquerade as empty."""
    table = _mock_quotes_table(None)
    with patch("src.bots.circuit_breaker_bot.get_client") as mock_db:
        mock_db.return_value.table.return_value = table
        with pytest.raises(RuntimeError, match="data=None"):
            circuit_breaker_bot._has_active_non_expired_quotes()


def test_invalidate_quotes_skips_on_chain_tx_when_no_active_quotes():
    with patch(
        "src.bots.circuit_breaker_bot._has_active_non_expired_quotes",
        return_value=False,
    ), patch("src.bots.circuit_breaker_bot.build_and_send_tx") as mock_tx:
        asyncio.run(circuit_breaker_bot.invalidate_quotes())

    mock_tx.assert_not_called()


def test_invalidate_quotes_sends_nonce_tx_when_active_quotes_exist():
    settler = MagicMock()
    tx_fn = MagicMock()
    settler.functions.incrementMakerNonce.return_value = tx_fn

    with patch(
        "src.bots.circuit_breaker_bot._has_active_non_expired_quotes",
        return_value=True,
    ), patch("src.bots.circuit_breaker_bot.get_operator_account"), patch(
        "src.bots.circuit_breaker_bot.get_batch_settler",
        return_value=settler,
    ), patch(
        "src.bots.circuit_breaker_bot.build_and_send_tx",
        return_value="0xabc",
    ) as mock_tx, patch(
        "src.bots.circuit_breaker_bot._deactivate_active_quotes",
        return_value=1,
    ):
        asyncio.run(circuit_breaker_bot.invalidate_quotes())

    mock_tx.assert_called_once()


def test_invalidate_quotes_sends_tx_on_every_trip_when_active_quotes_exist():
    """Repeated trips must send the tx each time — never trust in-memory state."""
    settler = MagicMock()
    settler.functions.incrementMakerNonce.return_value = MagicMock()

    with patch(
        "src.bots.circuit_breaker_bot._has_active_non_expired_quotes",
        return_value=True,
    ), patch("src.bots.circuit_breaker_bot.get_operator_account"), patch(
        "src.bots.circuit_breaker_bot.get_batch_settler",
        return_value=settler,
    ), patch(
        "src.bots.circuit_breaker_bot.build_and_send_tx",
        return_value="0xabc",
    ) as mock_tx, patch(
        "src.bots.circuit_breaker_bot._deactivate_active_quotes",
        return_value=1,
    ):
        asyncio.run(circuit_breaker_bot.invalidate_quotes())
        asyncio.run(circuit_breaker_bot.invalidate_quotes())
        asyncio.run(circuit_breaker_bot.invalidate_quotes())

    # No memoization: three trips → three txs. This is the reorg-proof
    # behavior — in-memory "already invalidated" state never decides.
    assert mock_tx.call_count == 3


def test_invalidate_quotes_sends_tx_when_db_lookup_fails():
    """When DB inspection raises, the nonce tx MUST still be sent for safety."""
    with patch(
        "src.bots.circuit_breaker_bot._has_active_non_expired_quotes",
        side_effect=RuntimeError("DB down"),
    ), patch("src.bots.circuit_breaker_bot.get_operator_account"), patch(
        "src.bots.circuit_breaker_bot.get_batch_settler"
    ), patch(
        "src.bots.circuit_breaker_bot.build_and_send_tx",
        return_value="0xabc",
    ) as mock_tx, patch(
        "src.bots.circuit_breaker_bot._deactivate_active_quotes",
        return_value=0,
    ):
        asyncio.run(circuit_breaker_bot.invalidate_quotes())

    mock_tx.assert_called_once()


def test_invalidate_quotes_reraises_on_onchain_failure():
    """On-chain failure must propagate so the outer loop retries."""
    with patch(
        "src.bots.circuit_breaker_bot._has_active_non_expired_quotes",
        return_value=True,
    ), patch("src.bots.circuit_breaker_bot.get_operator_account"), patch(
        "src.bots.circuit_breaker_bot.get_batch_settler"
    ), patch(
        "src.bots.circuit_breaker_bot.build_and_send_tx",
        side_effect=RuntimeError("revert"),
    ):
        with pytest.raises(RuntimeError, match="revert"):
            asyncio.run(circuit_breaker_bot.invalidate_quotes())
