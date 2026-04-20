from unittest.mock import MagicMock, patch

import pytest

from src.bots import event_indexer


@pytest.fixture(autouse=True)
def _reset_otoken_cache():
    """Wipe the metadata cache before every test so tests don't leak."""
    event_indexer._otoken_metadata_cache.clear()
    yield
    event_indexer._otoken_metadata_cache.clear()


def test_enrich_with_otoken_metadata_uses_available_otokens_cache():
    """DB metadata avoids repeated on-chain oToken calls."""
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


def test_load_otoken_metadata_falls_back_to_chain_when_db_fields_null():
    """Null fields in available_otokens must not poison the result — use chain."""
    table = MagicMock()
    table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"strike_price": None, "expiry": 1773993600, "is_put": True}
    ]
    otoken = MagicMock()
    otoken.functions.strikePrice.return_value.call.return_value = 200_000_000_000
    otoken.functions.expiry.return_value.call.return_value = 1773993600
    otoken.functions.isPut.return_value.call.return_value = True

    with patch("src.bots.event_indexer.get_client") as mock_db, patch(
        "src.bots.event_indexer.get_otoken"
    ) as mock_get_otoken:
        mock_db.return_value.table.return_value = table
        mock_get_otoken.return_value = otoken
        metadata = event_indexer._load_otoken_metadata(
            "0x3333333333333333333333333333333333333333"
        )

    assert metadata == {
        "strike_price": 200_000_000_000,
        "expiry": 1773993600,
        "is_put": True,
    }
    mock_get_otoken.assert_called_once()


def test_build_delivery_event_data_uses_cached_metadata():
    """Delivery event builder must hit the cache, avoiding chain reads."""
    addr = "0x4444444444444444444444444444444444444444"
    event_indexer._otoken_metadata_cache[addr] = {
        "strike_price": 200_000_000_000,
        "expiry": 1773993600,
        "is_put": True,
    }
    ev = MagicMock()
    ev.args.oToken = addr
    ev.args.user = "0x5555555555555555555555555555555555555555"
    ev.args.contraAmount = 123
    ev.transactionHash.hex.return_value = "0xdeadbeef"

    with patch("src.bots.event_indexer.get_otoken") as mock_get_otoken:
        row = event_indexer._build_delivery_event_data(ev)

    assert row is not None
    assert row["otoken_address"] == addr
    assert row["delivered_amount"] == "123"
    mock_get_otoken.assert_not_called()


def test_enrich_returns_none_when_metadata_unavailable():
    """Failed enrichment signals callers to skip storage (no partial rows)."""
    table = MagicMock()
    table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    otoken = MagicMock()
    otoken.functions.strikePrice.return_value.call.side_effect = RuntimeError("RPC")

    with patch("src.bots.event_indexer.get_client") as mock_db, patch(
        "src.bots.event_indexer.get_otoken"
    ) as mock_get_otoken:
        mock_db.return_value.table.return_value = table
        mock_get_otoken.return_value = otoken
        result = event_indexer._enrich_with_otoken_metadata(
            {"otoken_address": "0x7777777777777777777777777777777777777777"}
        )

    assert result is None


def test_store_events_refuses_incomplete_rows():
    """Defensive guard: never upsert a row missing settlement-critical fields."""
    with pytest.raises(ValueError, match="missing strike_price"):
        event_indexer._store_events(
            [
                {
                    "tx_hash": "0xabc",
                    "otoken_address": "0x1234",
                    "strike_price": None,
                    "expiry": 1773993600,
                    "is_put": True,
                }
            ]
        )


def test_fetch_and_store_skips_events_with_failed_enrichment():
    """Events whose enrichment returned None must not reach _store_events."""
    ev_good = MagicMock()
    ev_good.transactionHash.hex.return_value = "0xaaa"
    ev_good.blockNumber = 1
    ev_good.logIndex = 0
    ev_good.args.user = "0xuser1"
    ev_good.args.mm = "0xmm1"
    ev_good.args.oToken = "0xOK"
    ev_good.args.amount = 1
    ev_good.args.grossPremium = 1
    ev_good.args.netPremium = 1
    ev_good.args.fee = 0
    ev_good.args.collateral = 1
    ev_good.args.vaultId = 1

    ev_bad = MagicMock()
    ev_bad.transactionHash.hex.return_value = "0xbbb"
    ev_bad.blockNumber = 2
    ev_bad.logIndex = 0
    ev_bad.args.user = "0xuser2"
    ev_bad.args.mm = "0xmm2"
    ev_bad.args.oToken = "0xBAD"
    ev_bad.args.amount = 1
    ev_bad.args.grossPremium = 1
    ev_bad.args.netPremium = 1
    ev_bad.args.fee = 0
    ev_bad.args.collateral = 1
    ev_bad.args.vaultId = 2

    settler = MagicMock()
    settler.events.OrderExecuted.get_logs.return_value = [ev_good, ev_bad]

    def enrich(event_data):
        if event_data["otoken_address"] == "0xok":
            event_data["strike_price"] = 1
            event_data["expiry"] = 1
            event_data["is_put"] = True
            return event_data
        return None

    with patch(
        "src.bots.event_indexer._enrich_with_otoken_metadata", side_effect=enrich
    ), patch("src.bots.event_indexer._store_events", return_value=1) as mock_store, patch(
        "src.bots.event_indexer._notify_mm"
    ):
        event_indexer._fetch_and_store_order_events(settler, 1, 2)

    stored = mock_store.call_args[0][0]
    assert len(stored) == 1
    assert stored[0]["tx_hash"] == "0xaaa"


def test_otoken_metadata_cache_is_bounded():
    """Cache must evict oldest entries when size cap is exceeded."""
    original_max = event_indexer._OTOKEN_CACHE_MAX
    event_indexer._OTOKEN_CACHE_MAX = 3

    table = MagicMock()
    table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"strike_price": "2000.0", "expiry": 1773993600, "is_put": False}
    ]
    try:
        with patch("src.bots.event_indexer.get_client") as mock_db:
            mock_db.return_value.table.return_value = table
            for i in range(5):
                event_indexer._load_otoken_metadata(f"0x{i:040x}")
        assert len(event_indexer._otoken_metadata_cache) == 3
        assert f"0x{0:040x}" not in event_indexer._otoken_metadata_cache
        assert f"0x{4:040x}" in event_indexer._otoken_metadata_cache
    finally:
        event_indexer._OTOKEN_CACHE_MAX = original_max
