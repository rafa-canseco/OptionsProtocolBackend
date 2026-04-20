import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.bots import circuit_breaker_bot


def _mock_quotes_table(rows):
    table = MagicMock()
    table.select.return_value.eq.return_value.gt.return_value.order.return_value.limit.return_value.execute.return_value.data = rows
    return table


def test_active_quote_marker_returns_none_without_active_quotes():
    table = _mock_quotes_table([])
    with patch("src.bots.circuit_breaker_bot.get_client") as mock_db:
        mock_db.return_value.table.return_value = table
        assert circuit_breaker_bot._active_quote_marker() is None


def test_active_quote_marker_uses_newest_quote_identity():
    table = _mock_quotes_table([{"id": "quote-1", "created_at": "2026-04-20T00:00:00Z"}])
    with patch("src.bots.circuit_breaker_bot.get_client") as mock_db:
        mock_db.return_value.table.return_value = table
        assert (
            circuit_breaker_bot._active_quote_marker()
            == "quote-1:2026-04-20T00:00:00Z"
        )


def test_invalidate_quotes_skips_on_chain_tx_when_no_active_quotes():
    circuit_breaker_bot._last_invalidated_quote_marker = None
    with patch(
        "src.bots.circuit_breaker_bot._active_quote_marker", return_value=None
    ), patch("src.bots.circuit_breaker_bot.build_and_send_tx") as mock_tx:
        asyncio.run(circuit_breaker_bot.invalidate_quotes())

    mock_tx.assert_not_called()


def test_invalidate_quotes_does_not_repeat_nonce_tx_for_same_quote_generation():
    circuit_breaker_bot._last_invalidated_quote_marker = "quote-1:ts"
    with patch(
        "src.bots.circuit_breaker_bot._active_quote_marker",
        return_value="quote-1:ts",
    ), patch("src.bots.circuit_breaker_bot.build_and_send_tx") as mock_tx, patch(
        "src.bots.circuit_breaker_bot._deactivate_active_quotes",
        return_value=1,
    ):
        asyncio.run(circuit_breaker_bot.invalidate_quotes())

    mock_tx.assert_not_called()


def test_invalidate_quotes_sends_nonce_tx_for_new_active_quotes():
    circuit_breaker_bot._last_invalidated_quote_marker = None
    settler = MagicMock()
    tx_fn = MagicMock()
    settler.functions.incrementMakerNonce.return_value = tx_fn

    with patch(
        "src.bots.circuit_breaker_bot._active_quote_marker",
        return_value="quote-1:ts",
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
    assert circuit_breaker_bot._last_invalidated_quote_marker == "quote-1:ts"
