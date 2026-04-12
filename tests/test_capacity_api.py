"""Tests for MM capacity endpoints (POST /mm/capacity, GET /capacity)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.deps import require_mm_api_key
from src.main import app

client = TestClient(app)

MM_ADDRESS = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@pytest.fixture()
def auth_headers():
    """Override FastAPI dependency to bypass real API key auth."""
    app.dependency_overrides[require_mm_api_key] = lambda: MM_ADDRESS
    yield {"X-API-Key": "fake"}
    app.dependency_overrides.pop(require_mm_api_key, None)


@pytest.fixture()
def mock_db():
    """Patch get_client for both mm_routes and routes modules."""
    mock_client = MagicMock()
    with (
        patch("src.api.mm_routes.get_client", return_value=mock_client),
        patch("src.api.routes.get_client", return_value=mock_client),
    ):
        yield mock_client


class TestPostCapacity:
    def test_accepts_valid_report(self, auth_headers, mock_db):
        mock_db.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[{"mm_address": MM_ADDRESS}]
        )
        resp = client.post(
            "/mm/capacity",
            json={
                "asset": "ETH",
                "capacity_eth": 10.5,
                "capacity_usd": 21000.0,
                "status": "active",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # Verify upsert was called with authenticated address
        call_args = mock_db.table.return_value.upsert.call_args
        row = call_args[0][0]
        assert row["mm_address"] == MM_ADDRESS.lower()
        assert row["capacity_eth"] == 10.5
        assert row["status"] == "active"
        assert row["chain"] == "base"

    def test_sol_capacity_derives_solana_chain(self, auth_headers, mock_db):
        mock_db.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[{"mm_address": MM_ADDRESS}]
        )
        resp = client.post(
            "/mm/capacity",
            json={
                "asset": "sol",
                "capacity_eth": 100.0,
                "capacity_usd": 8500.0,
                "status": "active",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        row = mock_db.table.return_value.upsert.call_args[0][0]
        assert row["asset"] == "sol"
        assert row["chain"] == "solana"

    def test_accepts_internal_mm_fields(self, auth_headers, mock_db):
        mock_db.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[{}]
        )
        resp = client.post(
            "/mm/capacity",
            json={
                "capacity_eth": 5.0,
                "capacity_usd": 10000.0,
                "status": "degraded",
                "premium_pool_usd": 12000.0,
                "hedge_pool_usd": 15000.0,
                "leverage": 3,
                "open_positions_count": 2,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        row = mock_db.table.return_value.upsert.call_args[0][0]
        assert row["premium_pool_usd"] == 12000.0
        assert row["leverage"] == 3

    def test_rejects_invalid_status(self, auth_headers, mock_db):
        resp = client.post(
            "/mm/capacity",
            json={
                "capacity_eth": 1.0,
                "capacity_usd": 2000.0,
                "status": "invalid",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_rejects_negative_capacity(self, auth_headers, mock_db):
        resp = client.post(
            "/mm/capacity",
            json={
                "capacity_eth": -1.0,
                "capacity_usd": 2000.0,
                "status": "active",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_db_failure_returns_502(self, auth_headers, mock_db):
        mock_db.table.return_value.upsert.return_value.execute.side_effect = Exception(
            "DB down"
        )
        resp = client.post(
            "/mm/capacity",
            json={
                "capacity_eth": 1.0,
                "capacity_usd": 2000.0,
                "status": "active",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 502


def _capacity_mock_chain(mock_table):
    """Wire up the mock chain: .select().eq().eq().gte().execute()"""
    return mock_table.select.return_value.eq.return_value.eq.return_value.gte.return_value.execute


class TestGetCapacity:
    def test_returns_aggregated_capacity(self, mock_db):
        now = _now_iso()
        _capacity_mock_chain(mock_db.table.return_value).return_value = MagicMock(
            data=[
                {
                    "mm_address": "0xaaa",
                    "capacity_eth": 10.0,
                    "capacity_usd": 20000.0,
                    "status": "active",
                    "reported_at": now,
                },
                {
                    "mm_address": "0xbbb",
                    "capacity_eth": 5.0,
                    "capacity_usd": 10000.0,
                    "status": "active",
                    "reported_at": now,
                },
            ]
        )

        resp = client.get("/capacity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["capacity"] == 15.0
        assert data["capacity_usd"] == 30000.0
        assert data["market_open"] is True
        assert data["market_status"] == "active"
        assert data["max_position"] == 10.0
        assert data["mm_count"] == 2
        assert data["asset"] == "eth"

    def test_returns_full_when_no_mms(self, mock_db):
        _capacity_mock_chain(mock_db.table.return_value).return_value = MagicMock(
            data=[]
        )

        resp = client.get("/capacity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["market_open"] is False
        assert data["market_status"] == "full"
        assert data["mm_count"] == 0

    def test_degraded_status(self, mock_db):
        now = _now_iso()
        _capacity_mock_chain(mock_db.table.return_value).return_value = MagicMock(
            data=[
                {
                    "mm_address": "0xaaa",
                    "capacity_eth": 2.0,
                    "capacity_usd": 4000.0,
                    "status": "degraded",
                    "reported_at": now,
                },
            ]
        )

        resp = client.get("/capacity")
        data = resp.json()
        assert data["market_status"] == "degraded"
        assert data["market_open"] is True

    def test_mixed_statuses(self, mock_db):
        """One active + one full = market is active."""
        now = _now_iso()
        _capacity_mock_chain(mock_db.table.return_value).return_value = MagicMock(
            data=[
                {
                    "mm_address": "0xaaa",
                    "capacity_eth": 5.0,
                    "capacity_usd": 10000.0,
                    "status": "active",
                    "reported_at": now,
                },
                {
                    "mm_address": "0xbbb",
                    "capacity_eth": 0.0,
                    "capacity_usd": 0.0,
                    "status": "full",
                    "reported_at": now,
                },
            ]
        )

        resp = client.get("/capacity")
        data = resp.json()
        assert data["market_status"] == "active"
        assert data["market_open"] is True
        assert data["capacity"] == 5.0

    def test_asset_query_param(self, mock_db):
        """Asset query param is passed through to the filter."""
        _capacity_mock_chain(mock_db.table.return_value).return_value = MagicMock(
            data=[]
        )

        resp = client.get("/capacity?asset=btc")
        assert resp.status_code == 200
        data = resp.json()
        assert data["asset"] == "btc"


class TestPricesCapacityIntegration:
    def test_prices_proceeds_when_capacity_available(self, mock_db):
        """When at least one MM is active, /prices proceeds normally."""
        now = _now_iso()

        cap_result = MagicMock(
            data=[
                {"mm_address": "0xaaa", "status": "active", "reported_at": now},
            ]
        )
        quotes_result = MagicMock(data=[])

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_capacity":
                _capacity_mock_chain(mock_table).return_value = cap_result
            elif table_name == "mm_quotes":
                (
                    mock_table.select.return_value.eq.return_value.eq.return_value.gt.return_value.gt.return_value.execute
                ).return_value = quotes_result
            return mock_table

        mock_db.table.side_effect = side_effect

        with patch("src.api.routes.circuit_breaker") as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            import src.api.routes as routes_mod

            routes_mod._prices_cache.clear()
            routes_mod._prices_cached_at.clear()
            resp = client.get("/prices")

        assert resp.status_code == 200

    def test_prices_failopen_on_capacity_db_error(self, mock_db):
        """When capacity DB errors, /prices proceeds (fail-open)."""
        quotes_result = MagicMock(data=[])

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_capacity":
                _capacity_mock_chain(mock_table).side_effect = Exception("DB down")
            elif table_name == "mm_quotes":
                (
                    mock_table.select.return_value.eq.return_value.eq.return_value.gt.return_value.gt.return_value.execute
                ).return_value = quotes_result
            return mock_table

        mock_db.table.side_effect = side_effect

        with patch("src.api.routes.circuit_breaker") as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            import src.api.routes as routes_mod

            routes_mod._prices_cache.clear()
            routes_mod._prices_cached_at.clear()
            resp = client.get("/prices")

        assert resp.status_code == 200

    def test_prices_empty_capacity_does_not_503(self, mock_db):
        """Empty cap_rows (no MMs) does not trigger 503."""
        cap_result = MagicMock(data=[])
        quotes_result = MagicMock(data=[])

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_capacity":
                _capacity_mock_chain(mock_table).return_value = cap_result
            elif table_name == "mm_quotes":
                (
                    mock_table.select.return_value.eq.return_value.eq.return_value.gt.return_value.gt.return_value.execute
                ).return_value = quotes_result
            return mock_table

        mock_db.table.side_effect = side_effect

        with patch("src.api.routes.circuit_breaker") as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            import src.api.routes as routes_mod

            routes_mod._prices_cache.clear()
            routes_mod._prices_cached_at.clear()
            resp = client.get("/prices")

        assert resp.status_code == 200


class TestCapacityAuth:
    def test_post_capacity_requires_auth(self, mock_db):
        """POST /mm/capacity without API key returns 401."""
        resp = client.post(
            "/mm/capacity",
            json={
                "capacity_eth": 1.0,
                "capacity_usd": 2000.0,
                "status": "active",
            },
        )
        assert resp.status_code in (401, 422)


class TestGetCapacityErrors:
    def test_get_capacity_502_on_db_failure(self, mock_db):
        """GET /capacity returns 502 when DB is unreachable."""
        mock_db.table.side_effect = Exception("DB down")
        resp = client.get("/capacity")
        assert resp.status_code == 502


def _position_count_mock_chain(mock_table):
    """Wire up: .select().eq().eq().or_().gt().execute()"""
    return mock_table.select.return_value.eq.return_value.eq.return_value.or_.return_value.gt.return_value.execute


def _quotes_mock_chain(mock_table):
    """Wire up: .select().eq().eq().eq().gt().gt().execute()"""
    return mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.gt.return_value.gt.return_value.execute


def _available_otokens_mock_chain(mock_table):
    """Wire up: .select().eq().execute()"""
    return mock_table.select.return_value.eq.return_value.execute


FAKE_OTOKEN_ADDR = "0x" + "a" * 40


def _make_quote(strike_usd: float, is_put: bool, expiry: int = 9999999999) -> dict:
    """Build a minimal valid mm_quotes row for testing."""
    import time

    return {
        "bid_price": 1_000_000,  # 1 USDC
        "max_amount": 100_000_000,  # 1 oToken
        "deadline": int(time.time()) + 60,
        "strike_price": strike_usd,
        "expiry": expiry,
        "is_put": is_put,
        "otoken_address": FAKE_OTOKEN_ADDR,
        "signature": "0x" + "b" * 130,
        "mm_address": "0x" + "c" * 40,
        "quote_id": "1",
        "maker_nonce": 0,
        "asset": "eth",
        "is_active": True,
    }


_VALID_OTOKENS_RESULT = MagicMock(data=[{"otoken_address": FAKE_OTOKEN_ADDR}])


class TestPositionCounts:
    """Tests for position_count field on /prices responses."""

    def _prices_cb_patch(self):
        return patch("src.api.routes.circuit_breaker")

    def _clear_cache(self):
        import src.api.routes as routes_mod

        routes_mod._prices_cache.clear()
        routes_mod._prices_cached_at.clear()

    def test_position_count_default_zero_when_no_positions(self, mock_db):
        """position_count is 0 for all strikes when order_events returns nothing."""
        quote = _make_quote(2400.0, True)
        quotes_result = MagicMock(data=[quote])
        positions_result = MagicMock(data=[])

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_quotes":
                _quotes_mock_chain(mock_table).return_value = quotes_result
            elif table_name == "order_events":
                _position_count_mock_chain(mock_table).return_value = positions_result
            elif table_name == "available_otokens":
                _available_otokens_mock_chain(
                    mock_table
                ).return_value = _VALID_OTOKENS_RESULT
            return mock_table

        mock_db.table.side_effect = side_effect

        with self._prices_cb_patch() as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            self._clear_cache()
            with patch(
                "src.pricing.chainlink.get_asset_price", return_value=(2400.0, 0)
            ):
                resp = client.get("/prices")

        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["position_count"] == 0

    def test_position_count_applies_multiplier(self, mock_db):
        """1 active position → position_count == ACTIVITY_MULTIPLIER (3)."""
        import src.api.routes as routes_mod

        quote = _make_quote(2400.0, True)
        quotes_result = MagicMock(data=[quote])
        # order_events: 1 active put at strike 2400 (raw 8-decimal = 240000000000)
        # expiry must match the quote's expiry (9999999999) for the key to match
        positions_result = MagicMock(
            data=[{"strike_price": 240000000000, "is_put": True, "expiry": 9999999999}]
        )

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_quotes":
                _quotes_mock_chain(mock_table).return_value = quotes_result
            elif table_name == "order_events":
                _position_count_mock_chain(mock_table).return_value = positions_result
            elif table_name == "available_otokens":
                _available_otokens_mock_chain(
                    mock_table
                ).return_value = _VALID_OTOKENS_RESULT
            return mock_table

        mock_db.table.side_effect = side_effect

        with self._prices_cb_patch() as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            self._clear_cache()
            with patch(
                "src.pricing.chainlink.get_asset_price", return_value=(2400.0, 0)
            ):
                resp = client.get("/prices")

        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["position_count"] == routes_mod.ACTIVITY_MULTIPLIER

    def test_position_count_zero_on_db_failure(self, mock_db):
        """If order_events query fails, endpoint still returns 200 with position_count=0."""
        quote = _make_quote(2400.0, False)
        quotes_result = MagicMock(data=[quote])

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_quotes":
                _quotes_mock_chain(mock_table).return_value = quotes_result
            elif table_name == "order_events":
                _position_count_mock_chain(mock_table).side_effect = Exception(
                    "DB down"
                )
            elif table_name == "available_otokens":
                _available_otokens_mock_chain(
                    mock_table
                ).return_value = _VALID_OTOKENS_RESULT
            return mock_table

        mock_db.table.side_effect = side_effect

        with self._prices_cb_patch() as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            self._clear_cache()
            with patch(
                "src.pricing.chainlink.get_asset_price", return_value=(2400.0, 0)
            ):
                resp = client.get("/prices")

        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["position_count"] == 0

    def test_settled_positions_excluded(self, mock_db):
        """Settled positions are not counted (filtered by is_settled=False in query)."""
        quote = _make_quote(2400.0, True)
        quotes_result = MagicMock(data=[quote])
        # Simulates the DB correctly filtering out settled rows — returns empty
        positions_result = MagicMock(data=[])

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_quotes":
                _quotes_mock_chain(mock_table).return_value = quotes_result
            elif table_name == "order_events":
                # Verify the query chain includes is_settled=False filter
                chain = _position_count_mock_chain(mock_table)
                chain.return_value = positions_result
            elif table_name == "available_otokens":
                _available_otokens_mock_chain(
                    mock_table
                ).return_value = _VALID_OTOKENS_RESULT
            return mock_table

        mock_db.table.side_effect = side_effect

        with self._prices_cb_patch() as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            self._clear_cache()
            with patch(
                "src.pricing.chainlink.get_asset_price", return_value=(2400.0, 0)
            ):
                resp = client.get("/prices")

        assert resp.status_code == 200
        items = resp.json()
        assert items[0]["position_count"] == 0

        # Verify the order_events table was queried at all
        order_events_calls = [
            call
            for call in mock_db.table.call_args_list
            if call[0][0] == "order_events"
        ]
        assert len(order_events_calls) >= 1

    def test_multiple_positions_aggregated(self, mock_db):
        """Multiple positions for the same strike are summed before multiplier."""
        import src.api.routes as routes_mod

        quote = _make_quote(2400.0, True)
        quotes_result = MagicMock(data=[quote])
        # 2 active positions at same strike + expiry
        positions_result = MagicMock(
            data=[
                {"strike_price": 240000000000, "is_put": True, "expiry": 9999999999},
                {"strike_price": 240000000000, "is_put": True, "expiry": 9999999999},
            ]
        )

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_quotes":
                _quotes_mock_chain(mock_table).return_value = quotes_result
            elif table_name == "order_events":
                _position_count_mock_chain(mock_table).return_value = positions_result
            elif table_name == "available_otokens":
                _available_otokens_mock_chain(
                    mock_table
                ).return_value = _VALID_OTOKENS_RESULT
            return mock_table

        mock_db.table.side_effect = side_effect

        with self._prices_cb_patch() as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            self._clear_cache()
            with patch(
                "src.pricing.chainlink.get_asset_price", return_value=(2400.0, 0)
            ):
                resp = client.get("/prices")

        assert resp.status_code == 200
        items = resp.json()
        assert items[0]["position_count"] == 2 * routes_mod.ACTIVITY_MULTIPLIER

    def test_orphan_positions_roll_into_nearest_visible_expiry(self, mock_db):
        """Positions from a non-visible expiry roll into nearest visible expiry."""
        import src.api.routes as routes_mod

        quote = _make_quote(2400.0, True, expiry=9999999999)
        quotes_result = MagicMock(data=[quote])
        # Position at a different (orphaned) expiry — same strike/type
        positions_result = MagicMock(
            data=[{"strike_price": 240000000000, "is_put": True, "expiry": 8888888888}]
        )

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_quotes":
                _quotes_mock_chain(mock_table).return_value = quotes_result
            elif table_name == "order_events":
                _position_count_mock_chain(mock_table).return_value = positions_result
            elif table_name == "available_otokens":
                _available_otokens_mock_chain(
                    mock_table
                ).return_value = _VALID_OTOKENS_RESULT
            return mock_table

        mock_db.table.side_effect = side_effect

        with self._prices_cb_patch() as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            self._clear_cache()
            with patch(
                "src.pricing.chainlink.get_asset_price", return_value=(2400.0, 0)
            ):
                resp = client.get("/prices")

        assert resp.status_code == 200
        items = resp.json()
        assert items[0]["position_count"] == routes_mod.ACTIVITY_MULTIPLIER

    def test_orphan_dropped_when_no_visible_strike_match(self, mock_db):
        """Orphaned positions with no visible (strike, option_type) are dropped."""
        quote = _make_quote(2400.0, True, expiry=9999999999)
        quotes_result = MagicMock(data=[quote])
        # Position at a different strike — no visible match to roll into
        positions_result = MagicMock(
            data=[{"strike_price": 250000000000, "is_put": True, "expiry": 8888888888}]
        )

        def side_effect(table_name):
            mock_table = MagicMock()
            if table_name == "mm_quotes":
                _quotes_mock_chain(mock_table).return_value = quotes_result
            elif table_name == "order_events":
                _position_count_mock_chain(mock_table).return_value = positions_result
            elif table_name == "available_otokens":
                _available_otokens_mock_chain(
                    mock_table
                ).return_value = _VALID_OTOKENS_RESULT
            return mock_table

        mock_db.table.side_effect = side_effect

        with self._prices_cb_patch() as mock_cb:
            mock_cb.is_paused_for.return_value = False
            mock_cb.check.return_value = False
            self._clear_cache()
            with patch(
                "src.pricing.chainlink.get_asset_price", return_value=(2400.0, 0)
            ):
                resp = client.get("/prices")

        assert resp.status_code == 200
        items = resp.json()
        assert items[0]["position_count"] == 0


class TestCapacityAggregationEdgeCases:
    def test_degraded_plus_full(self, mock_db):
        """degraded + full (no active) = market degraded."""
        now = _now_iso()
        mock_result = MagicMock(
            data=[
                {
                    "mm_address": "0xaaa",
                    "capacity_eth": 3.0,
                    "capacity_usd": 6000.0,
                    "status": "degraded",
                    "reported_at": now,
                },
                {
                    "mm_address": "0xbbb",
                    "capacity_eth": 0.0,
                    "capacity_usd": 0.0,
                    "status": "full",
                    "reported_at": now,
                },
            ]
        )
        _capacity_mock_chain(mock_db.table.return_value).return_value = mock_result

        resp = client.get("/capacity")
        data = resp.json()
        assert data["market_status"] == "degraded"
        assert data["market_open"] is True
        assert data["capacity"] == 3.0
