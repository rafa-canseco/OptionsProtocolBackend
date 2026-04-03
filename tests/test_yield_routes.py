"""Tests for the yield API endpoints."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def _mock_yield_db(table_data: dict[str, list[dict]]):
    """Patch get_client to return different data per table."""
    mock_client = MagicMock()

    def _table(name):
        mock_table = MagicMock()
        chain = mock_table.select.return_value
        chain = chain.eq.return_value
        chain.order.return_value.limit.return_value.execute.return_value.data = (
            table_data.get(name, [])
        )
        chain.execute.return_value.data = table_data.get(name, [])
        return mock_table

    mock_client.table.side_effect = _table
    return patch("src.api.yield_routes.get_client", return_value=mock_client)


_ADDR = "0xaaaa000000000000000000000000000000000001"


def test_yield_summary_empty():
    """Empty allocations returns empty assets list."""
    with _mock_yield_db({"yield_allocations": []}):
        resp = client.get(f"/yield/user/{_ADDR}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet"] == _ADDR.lower()
    assert data["assets"] == []


def test_yield_summary_with_allocations():
    """Returns correct pending/delivered breakdown."""
    allocs = [
        {"asset": "usdc", "amount": 1000000, "status": "delivered"},
        {"asset": "usdc", "amount": 500000, "status": "pending"},
        {"asset": "eth", "amount": 100000000000000, "status": "delivered"},
    ]
    with _mock_yield_db({"yield_allocations": allocs}):
        resp = client.get(f"/yield/user/{_ADDR}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["assets"]) == 2


def test_yield_positions_empty():
    """No positions returns empty list."""
    with _mock_yield_db({"yield_positions": []}):
        resp = client.get(f"/yield/user/{_ADDR}/positions")
    assert resp.status_code == 200
    assert resp.json()["positions"] == []


def test_yield_positions_with_data():
    """Returns position data with is_active flag."""
    positions = [
        {
            "id": "p1",
            "vault_id": 42,
            "asset": "usdc",
            "collateral_amount": 1000000000,
            "deposited_at": "2026-04-02T10:00:00+00:00",
            "settled_at": None,
        },
        {
            "id": "p2",
            "vault_id": 43,
            "asset": "eth",
            "collateral_amount": 500000000000000000,
            "deposited_at": "2026-04-03T10:00:00+00:00",
            "settled_at": "2026-04-05T08:00:00+00:00",
        },
    ]
    with _mock_yield_db({"yield_positions": positions}):
        resp = client.get(f"/yield/user/{_ADDR}/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["positions"]) == 2
    assert data["positions"][0]["is_active"] is True
    assert data["positions"][1]["is_active"] is False


def test_yield_history_empty():
    """No history returns empty list."""
    with _mock_yield_db({"yield_allocations": []}):
        resp = client.get(f"/yield/user/{_ADDR}/history")
    assert resp.status_code == 200
    assert resp.json()["history"] == []


def test_yield_history_with_data():
    """Returns allocation history with human-readable amounts."""
    history = [
        {
            "id": "a1",
            "distribution_id": "d1",
            "asset": "usdc",
            "amount": 1500000,
            "status": "delivered",
            "airdrop_tx_hash": "0xabc",
            "created_at": "2026-04-13T08:00:00+00:00",
        }
    ]
    with _mock_yield_db({"yield_allocations": history}):
        resp = client.get(f"/yield/user/{_ADDR}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["history"]) == 1
    entry = data["history"][0]
    assert entry["amount"] == 1.5  # 1500000 / 1e6
    assert entry["airdrop_tx_hash"] == "0xabc"


def test_invalid_address_returns_400():
    """Invalid address returns 400 on all yield endpoints."""
    for path in [
        "/yield/user/bad",
        "/yield/user/bad/positions",
        "/yield/user/bad/history",
    ]:
        resp = client.get(path)
        assert resp.status_code == 400


@patch("src.api.yield_routes.get_margin_pool")
def test_yield_stats_empty(mock_pool):
    """Empty distributions returns zeroes for all assets."""
    mock_pool.return_value.functions.getAccruedYield.return_value.call.return_value = 0
    with _mock_yield_db({"yield_distributions": []}):
        resp = client.get("/yield/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["assets"]) == 3  # usdc, eth, btc
    for asset in data["assets"]:
        assert asset["total_yield"] == 0.0
        assert asset["distributions"] == 0
