"""Tests for the yield API endpoints."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

_ADDR = "0xaaaa000000000000000000000000000000000001"


def _patch_yield_deps(
    alloc_rows=None,
    pos_rows=None,
    all_pos_rows=None,
    dist_rows=None,
    accrued=None,
    last_dist_end=None,
):
    """Build patches for yield route dependencies."""
    if alloc_rows is None:
        alloc_rows = []
    if pos_rows is None:
        pos_rows = []
    if all_pos_rows is None:
        all_pos_rows = pos_rows
    if dist_rows is None:
        dist_rows = []
    if accrued is None:
        accrued = {"usdc": 0, "eth": 0, "btc": 0}

    mock_client = MagicMock()

    def _table(name):
        mock_table = MagicMock()
        if name == "yield_allocations":
            chain = mock_table.select.return_value.eq.return_value
            chain.execute.return_value.data = alloc_rows
            chain.order.return_value.limit.return_value.execute.return_value.data = (
                alloc_rows
            )
        elif name == "yield_positions":
            chain = mock_table.select.return_value.eq.return_value
            chain.execute.return_value.data = pos_rows
            chain.order.return_value.limit.return_value.execute.return_value.data = (
                pos_rows
            )
            chain.lt.return_value.execute.return_value.data = all_pos_rows
            # For the .limit(10000) all-positions query
            mock_table.select.return_value.limit.return_value.execute.return_value.data = all_pos_rows
        elif name == "yield_distributions":
            mock_table.select.return_value.execute.return_value.data = dist_rows
            mock_table.select.return_value.order.return_value.limit.return_value.execute.return_value.data = []
        return mock_table

    mock_client.table.side_effect = _table

    patches = [
        patch("src.api.yield_routes.get_client", return_value=mock_client),
        patch("src.api.yield_routes._get_accrued_yield", return_value=accrued),
    ]
    if last_dist_end:
        patches.append(
            patch(
                "src.api.yield_routes._get_last_distribution_end",
                return_value=last_dist_end,
            )
        )

    from contextlib import ExitStack

    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


def test_yield_summary_empty():
    """Empty allocations returns empty assets list."""
    with _patch_yield_deps():
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
    with _patch_yield_deps(alloc_rows=allocs):
        resp = client.get(f"/yield/user/{_ADDR}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["assets"]) == 2


def test_yield_positions_empty():
    """No positions returns empty list."""
    with _patch_yield_deps():
        resp = client.get(f"/yield/user/{_ADDR}/positions")
    assert resp.status_code == 200
    assert resp.json()["positions"] == []


def test_yield_positions_with_data():
    """Returns position data with is_active flag and estimated yield."""
    positions = [
        {
            "id": "p1",
            "vault_id": 42,
            "asset": "usdc",
            "collateral_amount": 1000000000,
            "deposited_at": "2026-04-02T10:00:00+00:00",
            "settled_at": None,
            "user_address": _ADDR.lower(),
        },
        {
            "id": "p2",
            "vault_id": 43,
            "asset": "eth",
            "collateral_amount": 500000000000000000,
            "deposited_at": "2026-04-03T10:00:00+00:00",
            "settled_at": "2026-04-05T08:00:00+00:00",
            "user_address": _ADDR.lower(),
        },
    ]
    with _patch_yield_deps(pos_rows=positions, all_pos_rows=positions):
        resp = client.get(f"/yield/user/{_ADDR}/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["positions"]) == 2
    assert data["positions"][0]["is_active"] is True
    assert data["positions"][1]["is_active"] is False
    assert "estimated_yield" in data["positions"][0]
    assert "totals" in data


def test_yield_history_empty():
    """No history returns empty list."""
    with _patch_yield_deps():
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
    with _patch_yield_deps(alloc_rows=history):
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
@patch("src.api.yield_routes.get_client")
def test_yield_stats_empty(mock_client, mock_pool):
    """Empty distributions returns zeroes for all assets."""
    mock_pool.return_value.functions.getAccruedYield.return_value.call.return_value = 0
    mock_table = MagicMock()
    mock_table.select.return_value.execute.return_value.data = []
    mock_client.return_value.table.return_value = mock_table
    resp = client.get("/yield/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["assets"]) == 3  # usdc, eth, btc
    for asset in data["assets"]:
        assert asset["total_yield"] == 0.0
        assert asset["distributions"] == 0


def test_yield_summary_includes_estimated_accruing():
    """Summary includes estimated_accruing when on-chain yield is available."""
    positions = [
        {
            "id": "p1",
            "asset": "usdc",
            "collateral_amount": 1000000000,
            "deposited_at": "2026-04-02T10:00:00+00:00",
            "settled_at": None,
            "user_address": _ADDR.lower(),
        },
    ]
    # 1000 USDC accrued, this user is the only position
    with _patch_yield_deps(
        pos_rows=positions,
        all_pos_rows=positions,
        accrued={"usdc": 1000000, "eth": 0, "btc": 0},
    ):
        resp = client.get(f"/yield/user/{_ADDR}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["assets"]) == 1
    usdc = data["assets"][0]
    assert usdc["asset"] == "usdc"
    assert usdc["estimated_accruing_raw"] > 0
    assert usdc["estimated_accruing"] > 0
