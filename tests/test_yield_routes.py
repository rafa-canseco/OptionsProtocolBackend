"""Focused tests for bounded yield API reads."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import src.api.yield_routes as yield_routes
from src.main import app


client = TestClient(app)
_ADDR = "0xaaaa000000000000000000000000000000000001"
_OTHER = "0xbbbb000000000000000000000000000000000002"
_NOW = "2026-08-03T18:00:00Z"
_FINGERPRINT = "a" * 64
_REAL_GET_ACCRUED_YIELD = yield_routes._get_accrued_yield


class _RpcCall:
    def __init__(self, owner, name, params):
        self.owner = owner
        self.name = name
        self.params = params

    def execute(self):
        self.owner.calls.append((self.name, self.params))
        response = self.owner.responses[self.name]
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(data=response)


class _RpcClient:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def rpc(self, name, params):
        return _RpcCall(self, name, params)

    def table(self, _name):
        raise AssertionError("bounded yield routes must not use table reads")


@pytest.fixture(autouse=True)
def reset_yield_caches(monkeypatch):
    yield_routes._accrued_cache = None
    yield_routes._stats_cache = None
    monkeypatch.setattr(
        "src.api.position_pagination.settings.position_cursor_secret",
        "unit-only-b1n432-cursor-secret-at-least-32-bytes",
    )
    monkeypatch.setattr(
        yield_routes,
        "_get_accrued_yield",
        lambda: {"usdc": 0, "eth": 0, "btc": 0},
    )
    monkeypatch.setattr(
        yield_routes,
        "_utc_now",
        lambda: datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc),
    )


def _summary_payload(rows=None):
    return {
        "rows": rows or [],
        "period_start": "2026-08-01T00:00:00Z",
        "as_of": _NOW,
    }


def _position_row(index, **overrides):
    row = {
        "id": str(uuid4()),
        "vault_id": index,
        "asset": "usdc",
        "collateral_amount": "1000000",
        "deposited_at": f"2026-08-03T17:{59 - index:02d}:00Z",
        "settled_at": None,
        "position_weight": "100",
        "global_weight": "400",
    }
    row.update(overrides)
    return row


def _history_row(index, **overrides):
    row = {
        "id": str(uuid4()),
        "distribution_id": str(uuid4()),
        "asset": "usdc",
        "amount": "1500000",
        "status": "delivered",
        "airdrop_tx_hash": "0xabc",
        "created_at": "2026-08-03T17:00:00Z",
    }
    row.update(overrides)
    return row


def _page_payload(rows, totals=None):
    return {
        "rows": rows,
        "totals": totals or [],
        "watermark": _NOW,
        "wallet_fingerprint": _FINGERPRINT,
        "period_start": "2026-08-01T00:00:00Z",
        "as_of": _NOW,
    }


def test_yield_summary_empty_is_one_bounded_rpc(monkeypatch):
    rpc_client = _RpcClient(b1nary_yield_user_summary=_summary_payload())
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get(f"/yield/user/{_ADDR}")

    assert response.status_code == 200
    assert response.json() == {
        "wallet": _ADDR,
        "assets": [],
        "as_of": _NOW,
        "accrued_as_of": _NOW,
    }
    assert len(rpc_client.calls) == 1
    assert rpc_client.calls[0][0] == "b1nary_yield_user_summary"


def test_summary_uses_global_denominator_and_decimal_floor(monkeypatch):
    monkeypatch.setattr(
        yield_routes,
        "_get_accrued_yield",
        lambda: {"usdc": 1_000_003, "eth": 0, "btc": 0},
    )
    rpc_client = _RpcClient(
        b1nary_yield_user_summary=_summary_payload(
            [
                {
                    "asset": "usdc",
                    "pending_raw": "11",
                    "delivered_raw": "13",
                    "user_weight": "100",
                    "global_weight": "400",
                    "estimated_accruing_raw": "240000",
                }
            ]
        )
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get(f"/yield/user/{_ADDR}")

    assert response.status_code == 200
    asset = response.json()["assets"][0]
    assert asset["estimated_accruing_raw"] == 240_000
    assert asset["total_raw"] == 240_024
    assert rpc_client.calls[0][1]["p_accrued"]["usdc"] == 1_000_003
    assert rpc_client.calls[0][1]["p_protocol_fee_bps"] == 400


def test_summary_preserves_sum_of_multiple_per_position_floors(monkeypatch):
    monkeypatch.setattr(
        yield_routes,
        "_get_accrued_yield",
        lambda: {"usdc": 6, "eth": 0, "btc": 0},
    )
    rpc_client = _RpcClient(
        b1nary_yield_user_summary=_summary_payload(
            [
                {
                    "asset": "usdc",
                    "pending_raw": "0",
                    "delivered_raw": "0",
                    "user_weight": "2",
                    "global_weight": "3",
                    # distributable=5: floor(5/3) + floor(5/3) = 2,
                    # while flooring the aggregate share would incorrectly be 3.
                    "estimated_accruing_raw": "2",
                }
            ]
        )
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get(f"/yield/user/{_ADDR}")

    assert response.status_code == 200
    assert response.json()["assets"][0]["estimated_accruing_raw"] == 2


def test_summary_omits_zero_only_position_asset_for_compatibility(monkeypatch):
    rpc_client = _RpcClient(
        b1nary_yield_user_summary=_summary_payload(
            [
                {
                    "asset": "eth",
                    "pending_raw": "0",
                    "delivered_raw": "0",
                    "user_weight": "10",
                    "global_weight": "20",
                    "estimated_accruing_raw": "0",
                }
            ]
        )
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get(f"/yield/user/{_ADDR}")

    assert response.status_code == 200
    assert response.json()["assets"] == []


def test_positions_are_bounded_projected_and_paginated(monkeypatch):
    monkeypatch.setattr(
        yield_routes,
        "_get_accrued_yield",
        lambda: {"usdc": 1_000_000, "eth": 0, "btc": 0},
    )
    rows = [_position_row(0), _position_row(1)]
    rpc_client = _RpcClient(
        b1nary_yield_position_page=_page_payload(
            rows,
            totals=[{"asset": "usdc", "estimated_yield_raw": "479999"}],
        )
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get(f"/yield/user/{_ADDR}/positions?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert len(body["positions"]) == 1
    assert body["positions"][0]["estimated_yield_raw"] == 240_000
    assert body["positions"][0]["is_active"] is True
    assert body["totals"] == [
        {
            "asset": "usdc",
            "estimated_yield_raw": 479_999,
            "estimated_yield": 0.479999,
        }
    ]
    assert body["has_more"] is True
    assert body["next_cursor"].startswith("v1.")
    assert body["limit"] == 1
    assert len(rpc_client.calls) == 1
    assert set(rpc_client.calls[0][1]) == {
        "p_user_address",
        "p_limit",
        "p_cursor_at",
        "p_cursor_id",
        "p_watermark",
        "p_wallet_fingerprint",
        "p_as_of",
        "p_period_start",
        "p_accrued",
        "p_protocol_fee_bps",
    }


def test_positions_continuation_recovers_complete_signed_financial_snapshot(
    monkeypatch,
):
    first_values = {"usdc": 1_000_000, "eth": 17, "btc": None}
    monkeypatch.setattr(yield_routes, "_get_accrued_yield", lambda: first_values)
    rows = [_position_row(0), _position_row(1)]
    totals = [{"asset": "usdc", "estimated_yield_raw": "479999"}]
    first_client = _RpcClient(
        b1nary_yield_position_page=_page_payload(rows, totals=totals)
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: first_client)

    first = client.get(f"/yield/user/{_ADDR}/positions?limit=1")

    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    first_params = first_client.calls[0][1]
    assert first_params["p_as_of"] == _NOW
    assert first_params["p_watermark"] == _NOW
    assert first_params["p_period_start"] is None
    assert first_params["p_accrued"] == first_values
    assert len(cursor) < 1024

    # A continuation is a historical financial snapshot, not a new refresh.
    # It must remain coherent even after the live accrued cache has expired.
    yield_routes._accrued_cache = None
    monkeypatch.setattr(yield_routes.settings, "protocol_fee_bps", 999)
    monkeypatch.setattr(
        yield_routes,
        "_get_accrued_yield",
        MagicMock(side_effect=AssertionError("continuation refreshed accrued values")),
    )
    second_client = _RpcClient(
        b1nary_yield_position_page=_page_payload(
            [_position_row(2)],
            totals=totals,
        )
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: second_client)

    second = client.get(f"/yield/user/{_ADDR}/positions?limit=1&cursor={cursor}")

    assert second.status_code == 200
    params = second_client.calls[0][1]
    assert params["p_as_of"] == _NOW
    assert params["p_watermark"] == _NOW
    assert params["p_period_start"] == "2026-08-01T00:00:00Z"
    assert params["p_accrued"] == first_values
    assert params["p_protocol_fee_bps"] == first_params["p_protocol_fee_bps"]
    assert second.json()["as_of"] == _NOW
    assert second.json()["accrued_as_of"] == _NOW
    assert second.json()["positions"][0]["estimated_yield_raw"] == 240_000
    assert second.json()["totals"] == first.json()["totals"]


@pytest.mark.parametrize("field", ["as_of", "period_start"])
def test_positions_reject_rpc_financial_snapshot_mismatch(monkeypatch, field):
    rows = [_position_row(0), _position_row(1)]
    first_client = _RpcClient(b1nary_yield_position_page=_page_payload(rows))
    monkeypatch.setattr(yield_routes, "get_client", lambda: first_client)
    first = client.get(f"/yield/user/{_ADDR}/positions?limit=1")
    assert first.status_code == 200

    mismatched = _page_payload([])
    mismatched[field] = "2026-08-03T18:00:01Z"
    second_client = _RpcClient(b1nary_yield_position_page=mismatched)
    monkeypatch.setattr(yield_routes, "get_client", lambda: second_client)

    response = client.get(
        f"/yield/user/{_ADDR}/positions?limit=1&cursor={first.json()['next_cursor']}"
    )

    assert response.status_code == 502


def test_positions_reject_undeclared_mutable_as_of_without_rpc(monkeypatch):
    rpc_client = _RpcClient(b1nary_yield_position_page=_page_payload([]))
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get(f"/yield/user/{_ADDR}/positions?as_of={_NOW}")

    assert response.status_code == 400
    assert "controlled by the server" in response.json()["detail"]
    assert rpc_client.calls == []


def test_position_snapshot_cursor_is_bound_to_endpoint_and_wallet(monkeypatch):
    rows = [_position_row(0), _position_row(1)]
    first_client = _RpcClient(b1nary_yield_position_page=_page_payload(rows))
    monkeypatch.setattr(yield_routes, "get_client", lambda: first_client)
    first = client.get(f"/yield/user/{_ADDR}/positions?limit=1")
    cursor = first.json()["next_cursor"]
    assert first.status_code == 200

    other_client = _RpcClient(
        b1nary_yield_history_page=_page_payload([]),
        b1nary_yield_position_page=_page_payload([]),
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: other_client)

    wrong_wallet = client.get(f"/yield/user/{_OTHER}/positions?limit=1&cursor={cursor}")
    wrong_endpoint = client.get(f"/yield/user/{_ADDR}/history?limit=1&cursor={cursor}")

    assert wrong_wallet.status_code == 400
    assert wrong_endpoint.status_code == 400
    assert other_client.calls == []


def test_history_cursor_is_bound_to_endpoint_and_wallet(monkeypatch):
    rows = [_history_row(0), _history_row(1)]
    first_client = _RpcClient(b1nary_yield_history_page=_page_payload(rows))
    monkeypatch.setattr(yield_routes, "get_client", lambda: first_client)
    first = client.get(f"/yield/user/{_ADDR}/history?limit=1")
    cursor = first.json()["next_cursor"]
    assert first.status_code == 200

    other_client = _RpcClient(
        b1nary_yield_history_page=_page_payload([]),
        b1nary_yield_position_page=_page_payload([]),
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: other_client)

    wrong_wallet = client.get(f"/yield/user/{_OTHER}/history?limit=1&cursor={cursor}")
    wrong_endpoint = client.get(
        f"/yield/user/{_ADDR}/positions?limit=1&cursor={cursor}"
    )

    assert wrong_wallet.status_code == 400
    assert wrong_endpoint.status_code == 400
    assert other_client.calls == []


def test_history_continuation_forwards_tied_uuid_key_and_watermark(monkeypatch):
    rows = [_history_row(0), _history_row(1)]
    first_client = _RpcClient(b1nary_yield_history_page=_page_payload(rows))
    monkeypatch.setattr(yield_routes, "get_client", lambda: first_client)
    first = client.get(f"/yield/user/{_ADDR}/history?limit=1")
    cursor = first.json()["next_cursor"]

    second_client = _RpcClient(b1nary_yield_history_page=_page_payload([]))
    monkeypatch.setattr(yield_routes, "get_client", lambda: second_client)
    second = client.get(f"/yield/user/{_ADDR}/history?limit=1&cursor={cursor}")

    assert second.status_code == 200
    params = second_client.calls[0][1]
    assert params["p_cursor_at"] == rows[0]["created_at"]
    assert params["p_cursor_id"] == rows[0]["id"]
    assert params["p_watermark"] == _NOW
    assert params["p_wallet_fingerprint"] == _FINGERPRINT


@pytest.mark.parametrize("path", ["positions", "history"])
@pytest.mark.parametrize("limit", [0, 101])
def test_paginated_limits_are_typed(path, limit):
    response = client.get(f"/yield/user/{_ADDR}/{path}?limit={limit}")
    assert response.status_code == 422


def test_invalid_address_returns_400():
    for path in [
        "/yield/user/bad",
        "/yield/user/bad/positions",
        "/yield/user/bad/history",
    ]:
        assert client.get(path).status_code == 400


def test_accrued_cache_reuses_zero_and_failure_values_until_expiry(monkeypatch):
    calls = 0

    def read():
        nonlocal calls
        calls += 1
        return {"usdc": 0, "eth": None, "btc": calls}

    clock = [100.0]
    monkeypatch.setattr(yield_routes, "_get_accrued_yield", read)
    monkeypatch.setattr(yield_routes.time, "monotonic", lambda: clock[0])

    first = yield_routes._get_accrued_snapshot()
    second = yield_routes._get_accrued_snapshot()
    clock[0] += 15.001
    third = yield_routes._get_accrued_snapshot()

    assert first is second
    assert first.values == {"usdc": 0, "eth": None, "btc": 1}
    assert third.values["btc"] == 2
    assert calls == 2


def test_accrued_reader_constructs_pool_once_and_isolates_assets(monkeypatch):
    pool = MagicMock()
    pool.functions.getAccruedYield.return_value.call.side_effect = [7, Exception(), 0]
    get_pool = MagicMock(return_value=pool)
    monkeypatch.setattr(yield_routes, "get_margin_pool", get_pool)

    values = _REAL_GET_ACCRUED_YIELD()

    assert values == {"usdc": 7, "eth": None, "btc": 0}
    assert get_pool.call_count == 1


def test_stats_cache_reuses_empty_result_and_refreshes_after_60s(monkeypatch):
    clock = [200.0]
    monkeypatch.setattr(yield_routes.time, "monotonic", lambda: clock[0])
    rpc_client = _RpcClient(
        b1nary_yield_stats={"rows": [], "as_of": _NOW},
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    first = client.get("/yield/stats")
    second = client.get("/yield/stats")
    clock[0] += 60.001
    third = client.get("/yield/stats")

    assert first.status_code == second.status_code == third.status_code == 200
    assert len(first.json()["assets"]) == 3
    assert len(rpc_client.calls) == 2


def test_stats_failure_is_cached_for_60s(monkeypatch):
    clock = [300.0]
    monkeypatch.setattr(yield_routes.time, "monotonic", lambda: clock[0])
    rpc_client = _RpcClient(b1nary_yield_stats=RuntimeError("offline"))
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    first = client.get("/yield/stats")
    second = client.get("/yield/stats")

    assert first.status_code == second.status_code == 502
    assert len(rpc_client.calls) == 1


def test_stats_preserves_raw_and_human_fields(monkeypatch):
    rpc_client = _RpcClient(
        b1nary_yield_stats={
            "rows": [
                {
                    "asset": "usdc",
                    "total_yield_raw": "1500000",
                    "total_fees_raw": "500000",
                    "distributions": 2,
                }
            ],
            "as_of": _NOW,
        }
    )
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get("/yield/stats")

    usdc = next(row for row in response.json()["assets"] if row["asset"] == "usdc")
    assert usdc["total_yield_raw"] == 1_500_000
    assert usdc["total_yield"] == 1.5
    assert usdc["total_fees"] == 0.5
    assert usdc["total_distributed"] == 1.0
    assert usdc["distributions"] == 2


def test_accrued_observation_precedes_reads_and_cache_expires_from_that_boundary(
    monkeypatch,
):
    observed_at = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)
    clock = [100.0]
    calls = 0

    def read():
        nonlocal calls
        calls += 1
        if calls == 1:
            clock[0] += 5.0
        return {"usdc": 0, "eth": None, "btc": calls}

    monkeypatch.setattr(yield_routes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(yield_routes, "_utc_now", lambda: observed_at)
    monkeypatch.setattr(yield_routes, "_get_accrued_yield", read)

    first = yield_routes._get_accrued_snapshot()
    clock[0] = 114.999
    cached = yield_routes._get_accrued_snapshot()
    clock[0] = 115.0
    refreshed = yield_routes._get_accrued_snapshot()

    assert first.as_of == _NOW
    assert first.expires_at == 115.0
    assert cached is first
    assert refreshed.values["btc"] == 2
    assert calls == 2


@pytest.mark.parametrize("duration", [15.0, 15.001])
def test_accrued_refresh_at_or_over_maximum_age_is_never_returned(
    monkeypatch,
    duration,
):
    clock = [200.0]

    def slow_read():
        clock[0] += duration
        return {"usdc": 0, "eth": None, "btc": 0}

    monkeypatch.setattr(yield_routes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(yield_routes, "_get_accrued_yield", slow_read)

    with pytest.raises(RuntimeError, match="refresh exceeded its maximum age"):
        yield_routes._get_accrued_snapshot()

    assert yield_routes._accrued_cache is None


def test_slow_accrued_refresh_returns_502_instead_of_stale_values(monkeypatch):
    clock = [300.0]

    def slow_read():
        clock[0] += 15.0
        return {"usdc": 0, "eth": None, "btc": 0}

    monkeypatch.setattr(yield_routes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(yield_routes, "_get_accrued_yield", slow_read)
    rpc_client = _RpcClient(b1nary_yield_user_summary=_summary_payload())
    monkeypatch.setattr(yield_routes, "get_client", lambda: rpc_client)

    response = client.get(f"/yield/user/{_ADDR}")

    assert response.status_code == 502
    assert rpc_client.calls == []
