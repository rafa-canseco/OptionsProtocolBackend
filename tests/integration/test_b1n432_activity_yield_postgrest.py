import hashlib
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from src.api.yield_pagination import build_yield_page, fetch_yield_page


pytestmark = pytest.mark.integration

PRIMARY = "0x0000000000000000000000000000000000000002"
ALSO = "0x0000000000000000000000000000000000000001"
ACTIVITY_EDGE = "0x0000000000000000000000000000000000000432"
ACTIVITY_FUTURE = "0x0000000000000000000000000000000000000433"
AS_OF = "2026-08-03T18:00:00Z"
ACCRUED = {"usdc": 1_000_003, "eth": 1_000_003, "btc": 1_000_003}
FEE_BPS = 400


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing integration environment: {name}")
    return value


@pytest.fixture(scope="module")
def postgrest_url() -> str:
    return _required_env("B1N432_POSTGREST_URL")


@pytest.fixture(scope="module")
def service_token() -> str:
    return _required_env("B1N432_SERVICE_TOKEN")


def _headers(
    token: str,
    *,
    prefer: str | None = None,
    accept: str | None = None,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if prefer:
        headers["Prefer"] = prefer
    if accept:
        headers["Accept"] = accept
    return headers


def _raw_rpc(postgrest_url, token, name, payload):
    return httpx.post(
        f"{postgrest_url}/rpc/{name}",
        headers=_headers(token),
        json=payload,
        timeout=60,
    )


def _table_count(postgrest_url, service_token, table):
    response = httpx.get(
        f"{postgrest_url}/{table}",
        headers=_headers(service_token, prefer="count=exact"),
        params={"select": "id", "limit": "1"},
        timeout=30,
    )
    response.raise_for_status()
    return int(response.headers["content-range"].rsplit("/", 1)[1])


@dataclass
class _RpcExecution:
    client: "_CountingRpcClient"
    name: str
    params: dict

    def execute(self):
        response = self.client.http.post(
            f"/rpc/{self.name}",
            headers=_headers(self.client.token),
            json=self.params,
        )
        self.client.request_count += 1
        self.client.response_bytes.append(len(response.content))
        response.raise_for_status()
        return SimpleNamespace(data=response.json())


class _CountingRpcClient:
    def __init__(self, postgrest_url, token):
        self.http = httpx.Client(base_url=postgrest_url, timeout=60)
        self.token = token
        self.request_count = 0
        self.response_bytes = []

    def rpc(self, name, params):
        return _RpcExecution(self, name, params)

    def close(self):
        self.http.close()


def _fixture_uuid(prefix: str, sequence_number: int) -> str:
    digest = hashlib.md5(
        f"{prefix}{sequence_number}".encode(), usedforsecurity=False
    ).hexdigest()
    return str(UUID(digest))


def _route_activity_metrics(payload: dict) -> dict:
    volume = round(float(payload["total_volume"]), 2)
    premium = round(float(payload["total_premium"]), 2)
    collateral = round(float(payload["total_collateral_usd"]), 2)
    return {
        "volume": volume,
        "premium": premium,
        "collateral": collateral,
        "earning_rate": round(premium / collateral, 6) if collateral > 0 else None,
        "active_days": payload["active_days"],
        "days_since_first": payload["days_since_first"],
        "position_count": payload["position_count"],
    }


@lru_cache(maxsize=1)
def _yield_fixture_oracle() -> tuple[dict[str, int], dict[str, int]]:
    global_weights: dict[str, int] = {}
    user_weights: dict[str, list[int]] = {}
    for sequence_number in range(1, 100_001):
        remainder = sequence_number % 100
        if remainder not in (98, 99):
            continue
        asset_remainder = sequence_number % 6
        asset = (
            "btc"
            if asset_remainder in (0, 3)
            else "eth"
            if asset_remainder in (1, 4)
            else "usdc"
        )
        collateral = (
            100_000_000 + sequence_number
            if asset == "btc"
            else 1_000_000_000_000_000_000 + sequence_number
            if asset == "eth"
            else 1_000_000 + sequence_number
        )
        fixture_seconds = sequence_number // 10
        duration = 86_400 + fixture_seconds if remainder == 98 else 237_600
        weight = collateral * duration
        global_weights[asset] = global_weights.get(asset, 0) + weight
        if sequence_number % 2 == 0:
            user_weights.setdefault(asset, []).append(weight)

    distributable = ACCRUED["usdc"] * (10_000 - FEE_BPS) // 10_000
    per_position_totals = {
        asset: sum(
            int(Decimal(distributable) * Decimal(weight) / global_weights[asset])
            for weight in weights
        )
        for asset, weights in user_weights.items()
    }
    aggregate_floors = {
        asset: int(
            Decimal(distributable)
            * Decimal(sum(weights))
            / Decimal(global_weights[asset])
        )
        for asset, weights in user_weights.items()
    }
    return per_position_totals, aggregate_floors


def _yield_summary_params() -> dict:
    return {
        "p_user_address": PRIMARY,
        "p_as_of": AS_OF,
        "p_accrued": ACCRUED,
        "p_protocol_fee_bps": FEE_BPS,
    }


def _yield_position_params(limit: int | None = 100) -> dict:
    return {
        "p_user_address": PRIMARY,
        "p_limit": limit,
        "p_cursor_at": None,
        "p_cursor_id": None,
        "p_watermark": None,
        "p_wallet_fingerprint": None,
        "p_as_of": AS_OF,
        "p_period_start": None,
        "p_accrued": ACCRUED,
        "p_protocol_fee_bps": FEE_BPS,
    }


def test_fixture_contains_100k_rows_per_unbounded_source(
    postgrest_url,
    service_token,
):
    assert _table_count(postgrest_url, service_token, "order_events") == 100_000
    assert _table_count(postgrest_url, service_token, "yield_positions") == 100_000
    assert _table_count(postgrest_url, service_token, "yield_allocations") == 100_000


def test_activity_100k_parity_is_one_row_below_4k(postgrest_url, service_token):
    response = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_activity_summary",
        {"p_addresses": [PRIMARY, ALSO]},
    )
    response.raise_for_status()
    body = response.json()
    metrics = _route_activity_metrics(body)

    assert isinstance(body, dict)
    assert len(response.content) < 4 * 1024
    # Ten rows are reserved for boundary fixtures. Of the remaining 99,990,
    # 9,999 belong to a third wallet.
    assert metrics == {
        "volume": 44_995.5,
        "premium": 149_985.0,
        "collateral": 89_991.0,
        "earning_rate": 1.666667,
        "active_days": 1,
        "days_since_first": 2,
        "position_count": 89_991,
    }
    print(
        "B1N-432 activity evidence: "
        f"source_rows=100000 rpc_rows=1 response_bytes={len(response.content)}"
    )


def test_activity_real_rpc_preserves_boundaries_dates_calls_and_fallbacks(
    postgrest_url,
    service_token,
):
    edge = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_activity_summary",
        {"p_addresses": [ACTIVITY_EDGE]},
    )
    future = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_activity_summary",
        {"p_addresses": [ACTIVITY_FUTURE]},
    )
    edge.raise_for_status()
    future.raise_for_status()

    # Covers Python's 2.675 tie behavior, future-date negatives, negative put
    # collateral, net=0/null premium fallback, ETH/BTC/default calls, stored
    # collateral rounding, all-chain inclusion, and UTC day buckets.
    assert _route_activity_metrics(edge.json()) == {
        "volume": 92_504.68,
        "premium": 3.2,
        "collateral": 2.67,
        "earning_rate": 1.198502,
        "active_days": 3,
        "days_since_first": 1,
        "position_count": 5,
    }
    assert _route_activity_metrics(future.json())["days_since_first"] == -2


def test_summary_positions_and_stats_have_fixed_cardinality_and_bytes(
    postgrest_url,
    service_token,
):
    summary = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_yield_user_summary",
        _yield_summary_params(),
    )
    positions = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_yield_position_page",
        _yield_position_params(),
    )
    stats = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_yield_stats",
        {},
    )
    for response in (summary, positions, stats):
        response.raise_for_status()

    summary_rows = summary.json()["rows"]
    position_rows = positions.json()["rows"]
    stats_rows = stats.json()["rows"]
    assert len(summary_rows) <= 3
    assert len(stats_rows) == 3
    assert len(position_rows) <= 101
    assert len(summary.content) < 16 * 1024
    assert len(stats.content) < 16 * 1024
    assert len(positions.content) < 128 * 1024

    for row in summary_rows:
        assert Decimal(row["global_weight"]) > Decimal(row["user_weight"]) > 0
    assert len(positions.json()["totals"]) <= 3
    print(
        "B1N-432 bounded response evidence: "
        f"summary_rows={len(summary_rows)} summary_bytes={len(summary.content)} "
        f"position_rows={len(position_rows)} position_bytes={len(positions.content)} "
        f"stats_rows={len(stats_rows)} stats_bytes={len(stats.content)}"
    )


def test_position_continuation_keeps_period_weights_and_totals_after_distribution(
    postgrest_url,
    service_token,
):
    first = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_yield_position_page",
        _yield_position_params(limit=1),
    )
    first.raise_for_status()
    first_body = first.json()
    assert len(first_body["rows"]) == 2
    lookahead = first_body["rows"][1]
    period_start = first_body["period_start"]
    inserted_id = "10000000-0000-4000-8000-000000000432"

    try:
        inserted = httpx.post(
            f"{postgrest_url}/yield_distributions",
            headers=_headers(service_token, prefer="return=representation"),
            json={
                "id": inserted_id,
                "harvest_tx_hash": "0xb1n432-continuation-boundary",
                "asset": "usdc",
                "total_yield": 432,
                "platform_fee": 17,
                "period_start": period_start,
                "period_end": "2026-08-02T00:00:00Z",
                "distributed_at": "2026-08-02T01:00:00Z",
            },
            timeout=30,
        )
        assert inserted.status_code == 201, inserted.text

        continuation_params = _yield_position_params(limit=1)
        continuation_params.update(
            {
                "p_cursor_at": first_body["rows"][0]["deposited_at"],
                "p_cursor_id": first_body["rows"][0]["id"],
                "p_watermark": first_body["watermark"],
                "p_wallet_fingerprint": first_body["wallet_fingerprint"],
                "p_period_start": period_start,
            }
        )
        continuation = _raw_rpc(
            postgrest_url,
            service_token,
            "b1nary_yield_position_page",
            continuation_params,
        )
        continuation.raise_for_status()
        continuation_body = continuation.json()

        assert continuation_body["period_start"] == period_start
        assert continuation_body["as_of"] == first_body["as_of"]
        assert continuation_body["totals"] == first_body["totals"]
        continued_row = continuation_body["rows"][0]
        assert continued_row["id"] == lookahead["id"]
        assert continued_row["position_weight"] == lookahead["position_weight"]
        assert continued_row["global_weight"] == lookahead["global_weight"]
        distributable = ACCRUED[continued_row["asset"]] * (10_000 - FEE_BPS) // 10_000
        first_estimate = int(
            Decimal(distributable)
            * Decimal(lookahead["position_weight"])
            / Decimal(lookahead["global_weight"])
        )
        continued_estimate = int(
            Decimal(distributable)
            * Decimal(continued_row["position_weight"])
            / Decimal(continued_row["global_weight"])
        )
        assert continued_estimate == first_estimate
    finally:
        deleted = httpx.delete(
            f"{postgrest_url}/yield_distributions",
            headers=_headers(service_token),
            params={"id": f"eq.{inserted_id}"},
            timeout=30,
        )
        assert deleted.status_code == 204, deleted.text


def test_summary_and_position_totals_sum_per_position_floors(
    postgrest_url,
    service_token,
):
    summary = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_yield_user_summary",
        _yield_summary_params(),
    )
    positions = _raw_rpc(
        postgrest_url,
        service_token,
        "b1nary_yield_position_page",
        _yield_position_params(limit=1),
    )
    summary.raise_for_status()
    positions.raise_for_status()

    expected, aggregate_floors = _yield_fixture_oracle()
    summary_totals = {
        row["asset"]: int(row["estimated_accruing_raw"])
        for row in summary.json()["rows"]
    }
    position_totals = {
        row["asset"]: int(row["estimated_yield_raw"])
        for row in positions.json()["totals"]
    }
    assert summary_totals == expected
    assert position_totals == expected
    assert len(positions.json()["rows"]) == 2  # public limit + lookahead
    assert any(expected[asset] != aggregate_floors[asset] for asset in expected)


def test_history_100k_snapshot_is_exact_once_across_ties_and_mutations(
    postgrest_url,
    service_token,
):
    rpc_client = _CountingRpcClient(postgrest_url, service_token)
    cursor = None
    seen = []
    new_id = "ffffffff-ffff-4fff-8fff-ffffffff0432"
    unseen_id = _fixture_uuid("b1n432-allocation-", 1)
    mutated = False
    started_at = time.perf_counter()
    try:
        while True:
            payload = fetch_yield_page(
                rpc_client,
                address=PRIMARY,
                stream="yield_history",
                limit=100,
                cursor=cursor,
            )
            page = build_yield_page(
                payload,
                address=PRIMARY,
                stream="yield_history",
                limit=100,
            )
            seen.extend(row["id"] for row in page["rows"])
            cursor = page["next_cursor"]

            if not mutated and rpc_client.request_count == 500:
                insert = httpx.post(
                    f"{postgrest_url}/yield_allocations",
                    headers=_headers(service_token, prefer="return=representation"),
                    json={
                        "id": new_id,
                        "distribution_id": "10000000-0000-4000-8000-000000000001",
                        "position_id": _fixture_uuid("b1n432-position-", 1),
                        "user_address": PRIMARY,
                        "asset": "usdc",
                        "amount": 432,
                        "status": "pending",
                    },
                    timeout=30,
                )
                assert insert.status_code == 201, insert.text
                update = httpx.patch(
                    f"{postgrest_url}/yield_allocations",
                    headers=_headers(service_token, prefer="return=representation"),
                    params={"id": f"eq.{unseen_id}"},
                    json={"status": "delivered"},
                    timeout=30,
                )
                assert update.status_code == 200, update.text
                mutated = True
            if cursor is None:
                break

        assert mutated is True
        assert rpc_client.request_count == 1_000
        assert len(seen) == 100_000
        assert len(set(seen)) == 100_000
        assert new_id not in seen
        assert seen.count(unseen_id) == 1
        max_response_bytes = max(rpc_client.response_bytes)
        assert max_response_bytes < 128 * 1024
        print(
            "B1N-432 history evidence: "
            f"rows={len(seen)} requests={rpc_client.request_count} "
            f"max_response_bytes={max_response_bytes} "
            f"duration_seconds={time.perf_counter() - started_at:.3f}"
        )

        fresh = fetch_yield_page(
            rpc_client,
            address=PRIMARY,
            stream="yield_history",
            limit=100,
        )
        assert new_id in {row["id"] for row in fresh["rows"]}
    finally:
        rpc_client.close()


@pytest.mark.parametrize(
    ("rpc_name", "payload"),
    [
        ("b1nary_activity_summary", {"p_addresses": [PRIMARY]}),
        (
            "b1nary_yield_user_summary",
            _yield_summary_params(),
        ),
        (
            "b1nary_yield_position_page",
            _yield_position_params(limit=1),
        ),
        (
            "b1nary_yield_history_page",
            {
                "p_user_address": PRIMARY,
                "p_limit": 1,
                "p_cursor_at": None,
                "p_cursor_id": None,
                "p_watermark": None,
                "p_wallet_fingerprint": None,
            },
        ),
        ("b1nary_yield_stats", {}),
    ],
)
def test_every_rpc_is_service_role_only(
    postgrest_url,
    service_token,
    rpc_name,
    payload,
):
    anonymous = _required_env("B1N432_ANON_TOKEN")
    authenticated = _required_env("B1N432_AUTHENTICATED_TOKEN")

    anon = _raw_rpc(postgrest_url, anonymous, rpc_name, payload)
    auth = _raw_rpc(postgrest_url, authenticated, rpc_name, payload)
    service = _raw_rpc(postgrest_url, service_token, rpc_name, payload)

    assert anon.status_code == 401
    assert auth.status_code == 403
    assert service.status_code == 200


@pytest.mark.parametrize(
    ("rpc_name", "payload"),
    [
        ("b1nary_yield_position_page", _yield_position_params(limit=None)),
        (
            "b1nary_yield_history_page",
            {
                "p_user_address": PRIMARY,
                "p_limit": None,
                "p_cursor_at": None,
                "p_cursor_id": None,
                "p_watermark": None,
                "p_wallet_fingerprint": None,
            },
        ),
    ],
)
def test_postgrest_rejects_null_page_limits(
    postgrest_url,
    service_token,
    rpc_name,
    payload,
):
    response = _raw_rpc(postgrest_url, service_token, rpc_name, payload)
    assert response.status_code == 400
    assert "limit must be between 1 and 100" in response.text


@pytest.mark.parametrize(
    ("kind", "index_names"),
    [
        ("history", ["idx_b1n432_yield_allocations_history"]),
        ("positions", ["idx_b1n432_yield_positions_user_page"]),
        (
            "overlap",
            [
                "idx_b1n432_yield_positions_overlap_active",
                "idx_b1n432_yield_positions_overlap_settled",
            ],
        ),
    ],
)
def test_real_equivalent_plans_use_b1n432_indexes(
    postgrest_url,
    service_token,
    kind,
    index_names,
):
    response = _raw_rpc(
        postgrest_url,
        service_token,
        "b1n432_fixture_index_plan",
        {"p_kind": kind},
    )
    response.raise_for_status()
    plan = "\n".join(response.json())
    for index_name in index_names:
        assert index_name in plan
    print(f"B1N-432 {kind} real-equivalent plan:\n{plan}")
