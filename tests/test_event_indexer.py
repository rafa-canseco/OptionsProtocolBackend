from unittest.mock import MagicMock, patch

from src.bots import event_indexer


def test_enrich_with_otoken_metadata_uses_available_otokens_cache():
    """DB metadata avoids repeated on-chain oToken calls."""
    event_indexer._otoken_metadata_cache.clear()
    table = MagicMock()
    table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {
            "strike_price": "2000.0",
            "expiry": 1773993600,
            "is_put": True,
        }
    ]

    with patch("src.bots.event_indexer.get_client") as mock_db, patch(
        "src.bots.event_indexer.get_otoken"
    ) as mock_get_otoken:
        mock_db.return_value.table.return_value = table
        first = event_indexer._enrich_with_otoken_metadata(
            {"otoken_address": "0x1111111111111111111111111111111111111111"}
        )
        second = event_indexer._enrich_with_otoken_metadata(
            {"otoken_address": "0x1111111111111111111111111111111111111111"}
        )

    assert first["strike_price"] == 200_000_000_000
    assert first["expiry"] == 1773993600
    assert first["is_put"] is True
    assert second == first
    mock_get_otoken.assert_not_called()


def test_enrich_with_otoken_metadata_falls_back_to_chain():
    """Missing DB metadata still preserves correctness via chain reads."""
    event_indexer._otoken_metadata_cache.clear()
    table = MagicMock()
    table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    otoken = MagicMock()
    otoken.functions.strikePrice.return_value.call.return_value = 200_000_000_000
    otoken.functions.expiry.return_value.call.return_value = 1773993600
    otoken.functions.isPut.return_value.call.return_value = False

    with patch("src.bots.event_indexer.get_client") as mock_db, patch(
        "src.bots.event_indexer.get_otoken"
    ) as mock_get_otoken:
        mock_db.return_value.table.return_value = table
        mock_get_otoken.return_value = otoken
        enriched = event_indexer._enrich_with_otoken_metadata(
            {"otoken_address": "0x2222222222222222222222222222222222222222"}
        )

    assert enriched["strike_price"] == 200_000_000_000
    assert enriched["expiry"] == 1773993600
    assert enriched["is_put"] is False
    mock_get_otoken.assert_called_once()
