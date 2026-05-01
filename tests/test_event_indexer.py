from unittest.mock import MagicMock, patch

import pytest

from src.bots import event_indexer


@pytest.fixture(autouse=True)
def _reset_otoken_cache():
    """Wipe the metadata cache and pending-failure state before every test."""
    event_indexer._otoken_metadata_cache.clear()
    event_indexer._pending_failure_block = None
    yield
    event_indexer._otoken_metadata_cache.clear()
    event_indexer._pending_failure_block = None


def _make_order_event(block_number: int, tx_hash: str, otoken: str = "0xOK"):
    ev = MagicMock()
    ev.transactionHash.hex.return_value = tx_hash
    ev.blockNumber = block_number
    ev.logIndex = 0
    ev.args.user = "0x" + "a" * 40
    ev.args.mm = "0x" + "b" * 40
    ev.args.oToken = otoken
    ev.args.amount = 1
    ev.args.grossPremium = 1
    ev.args.netPremium = 1
    ev.args.fee = 0
    ev.args.collateral = 1
    ev.args.vaultId = block_number
    return ev


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

    with (
        patch("src.bots.event_indexer.get_client") as mock_db,
        patch("src.bots.event_indexer.get_otoken") as mock_get_otoken,
    ):
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

    with (
        patch("src.bots.event_indexer.get_client") as mock_db,
        patch("src.bots.event_indexer.get_otoken") as mock_get_otoken,
    ):
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
    otoken.functions.underlying.return_value.call.return_value = (
        event_indexer.settings.weth_address
    )

    with (
        patch("src.bots.event_indexer.get_client") as mock_db,
        patch("src.bots.event_indexer.get_otoken") as mock_get_otoken,
    ):
        mock_db.return_value.table.return_value = table
        mock_get_otoken.return_value = otoken
        metadata = event_indexer._load_otoken_metadata(
            "0x3333333333333333333333333333333333333333"
        )

    assert metadata == {
        "strike_price": 200_000_000_000,
        "expiry": 1773993600,
        "is_put": True,
        "underlying": event_indexer.settings.weth_address.lower(),
        "asset": "eth",
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

    with (
        patch("src.bots.event_indexer.get_client") as mock_db,
        patch("src.bots.event_indexer.get_otoken") as mock_get_otoken,
    ):
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

    with (
        patch(
            "src.bots.event_indexer._enrich_with_otoken_metadata", side_effect=enrich
        ),
        patch("src.bots.event_indexer._store_events", return_value=1) as mock_store,
        patch("src.bots.event_indexer._notify_mm"),
    ):
        event_indexer._fetch_and_store_order_events(settler, 1, 2)

    stored = mock_store.call_args[0][0]
    assert len(stored) == 1
    assert stored[0]["tx_hash"] == "0xaaa"


def test_fetch_and_store_returns_first_failed_block_and_stops():
    """On failure, later events in the batch must not be stored either,
    preserving ordering and ensuring the cursor stops at the failure."""
    settler = MagicMock()
    settler.events.OrderExecuted.get_logs.return_value = [
        _make_order_event(10, "0xa", otoken="0xOK"),
        _make_order_event(12, "0xb", otoken="0xBAD"),
        _make_order_event(14, "0xc", otoken="0xOK"),
    ]

    def enrich(event_data):
        if event_data["otoken_address"] == "0xbad":
            return None
        event_data["strike_price"] = 1
        event_data["expiry"] = 1
        event_data["is_put"] = True
        return event_data

    with (
        patch(
            "src.bots.event_indexer._enrich_with_otoken_metadata", side_effect=enrich
        ),
        patch("src.bots.event_indexer._store_events", return_value=1) as mock_store,
        patch("src.bots.event_indexer._notify_mm"),
    ):
        stored, first_failed = event_indexer._fetch_and_store_order_events(
            settler, 10, 14
        )

    assert first_failed == 12
    stored_batch = mock_store.call_args[0][0]
    assert [e["tx_hash"] for e in stored_batch] == ["0xa"]
    assert stored == 1


def test_fetch_and_update_delivery_returns_first_failed_block_and_stops():
    """Delivery enrichment failures must also gate cursor advance."""
    settler = MagicMock()
    ev_bad = MagicMock()
    ev_bad.blockNumber = 20
    ev_bad.transactionHash.hex.return_value = "0xbad"
    ev_good = MagicMock()
    ev_good.blockNumber = 22
    ev_good.transactionHash.hex.return_value = "0xgood"
    settler.events.PhysicalDelivery.get_logs.return_value = [ev_bad, ev_good]

    def build(ev):
        if ev is ev_bad:
            return None
        return {
            "user_address": "0x1",
            "otoken_address": "0x2",
            "delivered_asset": "0x3",
            "delivered_amount": "1",
            "delivery_tx_hash": "0xgood",
        }

    with (
        patch("src.bots.event_indexer._build_delivery_event_data", side_effect=build),
        patch(
            "src.bots.event_indexer._update_delivery_events", return_value=0
        ) as mock_update,
    ):
        updated, first_failed = event_indexer._fetch_and_update_delivery_events(
            settler, 20, 22
        )

    assert first_failed == 20
    assert mock_update.call_args[0][0] == []
    assert updated == 0


def test_index_once_does_not_advance_past_failed_block():
    """Forward pass must clamp cursor to (first_failed - 1)."""
    w3 = MagicMock()
    w3.eth.block_number = 100
    settler = MagicMock()

    with (
        patch("src.bots.event_indexer.get_w3", return_value=w3),
        patch("src.bots.event_indexer.get_batch_settler", return_value=settler),
        patch("src.bots.event_indexer._get_last_indexed_block", return_value=49),
        patch("src.bots.event_indexer._set_last_indexed_block") as mock_set,
        patch(
            "src.bots.event_indexer._fetch_and_store_order_events",
            return_value=(0, 75),
        ),
        patch(
            "src.bots.event_indexer._fetch_and_update_delivery_events",
            return_value=(0, None),
        ),
    ):
        import asyncio

        asyncio.run(event_indexer.index_once())

    # from_block = 50, to_block = min(50+2000-1, 98) = 98, failure at 75.
    # safe_to_block = 74. Assert the cursor moved to 74, not 98.
    advance_calls = [c.args[0] for c in mock_set.call_args_list]
    assert 74 in advance_calls
    assert 98 not in advance_calls
    assert event_indexer._pending_failure_block == 75


def test_index_once_does_not_advance_when_first_block_fails():
    """If the very first block in the batch fails, cursor stays put."""
    w3 = MagicMock()
    w3.eth.block_number = 100
    settler = MagicMock()

    with (
        patch("src.bots.event_indexer.get_w3", return_value=w3),
        patch("src.bots.event_indexer.get_batch_settler", return_value=settler),
        patch("src.bots.event_indexer._get_last_indexed_block", return_value=49),
        patch("src.bots.event_indexer._set_last_indexed_block") as mock_set,
        patch(
            "src.bots.event_indexer._fetch_and_store_order_events",
            return_value=(0, 50),
        ),
        patch(
            "src.bots.event_indexer._fetch_and_update_delivery_events",
            return_value=(0, None),
        ),
    ):
        import asyncio

        asyncio.run(event_indexer.index_once())

    # Failure at from_block itself → the forward-pass branch of
    # _set_last_indexed_block must not be called with any value.
    forward_calls = mock_set.call_args_list
    # Rescan uses _set_last_indexed_block indirectly through fetchers only if
    # they raise; here neither raises, so no call expected from this path.
    assert forward_calls == []


def test_index_once_clears_pending_failure_when_catchup_succeeds():
    """A successful forward pass past the pending block clears the marker."""
    event_indexer._pending_failure_block = 70

    w3 = MagicMock()
    w3.eth.block_number = 100
    settler = MagicMock()

    with (
        patch("src.bots.event_indexer.get_w3", return_value=w3),
        patch("src.bots.event_indexer.get_batch_settler", return_value=settler),
        patch("src.bots.event_indexer._get_last_indexed_block", return_value=49),
        patch("src.bots.event_indexer._set_last_indexed_block"),
        patch(
            "src.bots.event_indexer._fetch_and_store_order_events",
            return_value=(5, None),
        ),
        patch(
            "src.bots.event_indexer._fetch_and_update_delivery_events",
            return_value=(0, None),
        ),
    ):
        import asyncio

        asyncio.run(event_indexer.index_once())

    assert event_indexer._pending_failure_block is None


def test_subscription_skip_does_not_advance_cursor_on_later_event():
    """After a subscription enrichment failure at block B, a later event
    at block B+N must not advance the cursor past B-1."""
    # Simulate prior failure at block 50.
    event_indexer._pending_failure_block = 50

    decoded = MagicMock()
    decoded.blockNumber = 60
    decoded.transactionHash.hex.return_value = "0x60"
    settler = MagicMock()
    settler.events.OrderExecuted.process_log.return_value = decoded

    with (
        patch(
            "src.bots.event_indexer._build_order_event_data",
            return_value={
                "tx_hash": "0x60",
                "block_number": 60,
                "otoken_address": "0x",
            },
        ),
        patch(
            "src.bots.event_indexer._enrich_with_otoken_metadata",
            side_effect=lambda ev: {
                **ev,
                "strike_price": 1,
                "expiry": 1,
                "is_put": True,
            },
        ),
        patch("src.bots.event_indexer._store_events", return_value=1),
        patch("src.bots.event_indexer._notify_mm"),
        patch("src.bots.event_indexer._set_last_indexed_block") as mock_set,
    ):
        event_indexer._process_order_subscription_log(settler, {"dummy": True})

    mock_set.assert_called_once_with(49)


def test_subscription_failure_records_pending_block():
    """Enrichment failure in subscription must set `_pending_failure_block`."""
    decoded = MagicMock()
    decoded.blockNumber = 77
    decoded.transactionHash.hex.return_value = "0x77"
    settler = MagicMock()
    settler.events.OrderExecuted.process_log.return_value = decoded

    with (
        patch(
            "src.bots.event_indexer._build_order_event_data",
            return_value={
                "tx_hash": "0x77",
                "block_number": 77,
                "otoken_address": "0x",
            },
        ),
        patch("src.bots.event_indexer._enrich_with_otoken_metadata", return_value=None),
        patch("src.bots.event_indexer._set_last_indexed_block") as mock_set,
    ):
        event_indexer._process_order_subscription_log(settler, {"dummy": True})

    assert event_indexer._pending_failure_block == 77
    mock_set.assert_not_called()


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


# ---------------------------------------------------------------------------
# _update_delivery_events — one event must claim exactly one row
# ---------------------------------------------------------------------------


class _FakeTable:
    """Minimal stub of the supabase chained-builder that records calls and
    returns canned data per (table, op_chain) signature."""

    def __init__(self, rows: list[dict]):
        # Each row simulates an order_events row; 'id' is unique.
        self.rows = list(rows)
        self.updates: list[tuple[str, dict]] = []

    def table(self, name):
        assert name == "order_events"
        return _Query(self)


class _Query:
    def __init__(self, parent: "_FakeTable"):
        self._p = parent
        self._filters: list[tuple[str, str, object]] = []
        self._op = None
        self._payload = None
        self._order = None
        self._limit = None

    def select(self, _fields):
        self._op = "select"
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def order(self, col):
        self._order = col
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        def matches(row):
            for kind, col, val in self._filters:
                if kind == "eq" and row.get(col) != val:
                    return False
                if kind == "is" and val == "null" and row.get(col) is not None:
                    return False
            return True

        matched = [r for r in self._p.rows if matches(r)]
        if self._order:
            matched.sort(key=lambda r: r.get(self._order, 0))
        if self._limit:
            matched = matched[: self._limit]
        if self._op == "update":
            for r in matched:
                self._p.updates.append((r["id"], dict(self._payload)))
                r.update(self._payload)
        return MagicMock(data=matched)


def _delivery_event(user, otoken, tx, contra="100", asset="usdc"):
    return {
        "user_address": user,
        "otoken_address": otoken,
        "delivered_asset": asset,
        "delivered_amount": contra,
        "delivery_tx_hash": tx,
    }


def test_update_delivery_events_three_events_to_three_rows_no_overwrite():
    """Three vaults of the same user on the same oToken: each PhysicalDelivery
    event must claim a distinct row. Pre-fix this overwrote all three rows
    with the last event's data.
    """
    rows = [
        {
            "id": "a",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 30,
            "delivery_tx_hash": None,
        },
        {
            "id": "b",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 31,
            "delivery_tx_hash": None,
        },
        {
            "id": "c",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 33,
            "delivery_tx_hash": None,
        },
    ]
    fake = _FakeTable(rows)
    events = [
        _delivery_event("0xu", "0xt", "0xtx30", contra="500"),
        _delivery_event("0xu", "0xt", "0xtx31", contra="60"),
        _delivery_event("0xu", "0xt", "0xtx33", contra="240"),
    ]
    with patch("src.bots.event_indexer.get_client", return_value=fake):
        n = event_indexer._update_delivery_events(events)

    assert n == 3
    by_id = {r["id"]: r for r in fake.rows}
    assert by_id["a"]["delivery_tx_hash"] == "0xtx30"
    assert by_id["a"]["delivered_amount"] == "500"
    assert by_id["b"]["delivery_tx_hash"] == "0xtx31"
    assert by_id["b"]["delivered_amount"] == "60"
    assert by_id["c"]["delivery_tx_hash"] == "0xtx33"
    assert by_id["c"]["delivered_amount"] == "240"


def test_update_delivery_events_idempotent_on_reindex():
    """Reprocessing already-recorded events must not write again."""
    rows = [
        {
            "id": "a",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 1,
            "delivery_tx_hash": "0xtxA",
            "delivered_amount": "100",
        },
    ]
    fake = _FakeTable(rows)
    events = [_delivery_event("0xu", "0xt", "0xtxA", contra="100")]
    with patch("src.bots.event_indexer.get_client", return_value=fake):
        n = event_indexer._update_delivery_events(events)

    assert n == 0
    assert fake.updates == []  # no writes


def test_update_delivery_events_no_unmarked_row_logs_warning(caplog):
    """If the bot already wrote per-vault hashes and the indexer sees a NEW
    event whose tx isn't in DB and no NULL row remains, log a warning rather
    than silently overwriting another vault's correct hash."""
    rows = [
        {
            "id": "a",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 1,
            "delivery_tx_hash": "0xtxA",
        },
    ]
    fake = _FakeTable(rows)
    events = [_delivery_event("0xu", "0xt", "0xtxOTHER", contra="100")]

    import logging

    with (
        patch("src.bots.event_indexer.get_client", return_value=fake),
        caplog.at_level(logging.WARNING, logger="src.bots.event_indexer"),
    ):
        n = event_indexer._update_delivery_events(events)

    assert n == 0
    assert fake.updates == []
    assert any("matched no unmarked DB row" in r.getMessage() for r in caplog.records)


def test_update_delivery_events_partial_pre_fill_claims_remaining():
    """Bot wrote hashes for some vaults; the indexer must claim only the
    remaining unmarked rows in vault_id order."""
    rows = [
        {
            "id": "a",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 1,
            "delivery_tx_hash": "0xtxBOT1",
        },
        {
            "id": "b",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 2,
            "delivery_tx_hash": None,
        },
        {
            "id": "c",
            "user_address": "0xu",
            "otoken_address": "0xt",
            "vault_id": 3,
            "delivery_tx_hash": None,
        },
    ]
    fake = _FakeTable(rows)
    events = [
        _delivery_event(
            "0xu", "0xt", "0xtxBOT1", contra="10"
        ),  # idempotent — already in DB
        _delivery_event("0xu", "0xt", "0xtxFROM_INDEXER_2", contra="20"),
        _delivery_event("0xu", "0xt", "0xtxFROM_INDEXER_3", contra="30"),
    ]
    with patch("src.bots.event_indexer.get_client", return_value=fake):
        n = event_indexer._update_delivery_events(events)

    assert n == 2
    by_id = {r["id"]: r for r in fake.rows}
    assert by_id["a"]["delivery_tx_hash"] == "0xtxBOT1"
    assert by_id["b"]["delivery_tx_hash"] == "0xtxFROM_INDEXER_2"
    assert by_id["c"]["delivery_tx_hash"] == "0xtxFROM_INDEXER_3"
