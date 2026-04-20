import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.bots import circuit_breaker_bot


@pytest.fixture(autouse=True)
def _reset_circuit_breaker_state():
    """Reset module-level state so tests don't leak into each other."""
    circuit_breaker_bot._last_invalidated_quote_marker = None
    yield
    circuit_breaker_bot._last_invalidated_quote_marker = None


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


def test_invalidate_quotes_sends_tx_when_db_lookup_fails():
    """When DB inspection raises, the nonce tx MUST still be sent for safety."""
    with patch(
        "src.bots.circuit_breaker_bot._active_quote_marker",
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
    # Marker must not be persisted when DB lookup failed — otherwise the next
    # cycle with a persistent DB outage would skip the tx entirely.
    assert circuit_breaker_bot._last_invalidated_quote_marker is None


def test_invalidate_quotes_resends_tx_on_repeated_db_failure():
    """Repeated DB failures must NOT be cached as a pseudo-marker."""
    with patch(
        "src.bots.circuit_breaker_bot._active_quote_marker",
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
        asyncio.run(circuit_breaker_bot.invalidate_quotes())

    assert mock_tx.call_count == 2


def test_invalidate_quotes_reraises_on_onchain_failure():
    """On-chain failure must propagate so the outer loop retries."""
    with patch(
        "src.bots.circuit_breaker_bot._active_quote_marker",
        return_value="quote-1:ts",
    ), patch("src.bots.circuit_breaker_bot.get_operator_account"), patch(
        "src.bots.circuit_breaker_bot.get_batch_settler"
    ), patch(
        "src.bots.circuit_breaker_bot.build_and_send_tx",
        side_effect=RuntimeError("revert"),
    ):
        with pytest.raises(RuntimeError, match="revert"):
            asyncio.run(circuit_breaker_bot.invalidate_quotes())

    # Marker must NOT be persisted when tx failed — next cycle must retry.
    assert circuit_breaker_bot._last_invalidated_quote_marker is None


def test_active_quote_marker_raises_when_supabase_returns_data_none():
    """Silent Supabase failure (data=None) must raise, not masquerade as empty."""
    table = MagicMock()
    table.select.return_value.eq.return_value.gt.return_value.order.return_value.limit.return_value.execute.return_value.data = None
    with patch("src.bots.circuit_breaker_bot.get_client") as mock_db:
        mock_db.return_value.table.return_value = table
        with pytest.raises(RuntimeError, match="data=None"):
            circuit_breaker_bot._active_quote_marker()
