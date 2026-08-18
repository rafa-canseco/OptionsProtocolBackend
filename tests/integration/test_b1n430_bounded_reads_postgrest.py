import os
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from src.api.position_pagination import (
    PositionCursorError,
    PositionSubject,
    build_position_page,
    build_position_snapshot,
    fetch_position_rpc,
    normalized_wallet_subject,
)
from src.api.routes import _price_count_series_batches


pytestmark = pytest.mark.integration

SCALE_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
SINGLE_ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"
TEST_WALLET = "0x0000000000000000000000000000000000000001"
WATERMARK_FLOOR = "2026-08-03T00:00:00Z"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing integration environment: {name}")
    return value


@pytest.fixture(scope="module")
def postgrest_url() -> str:
    return _required_env("B1N430_POSTGREST_URL")


@pytest.fixture(scope="module")
def service_token() -> str:
    return _required_env("B1N430_SERVICE_TOKEN")


def _headers(token: str, *, prefer: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if prefer:
        headers["Prefer"] = prefer
    return headers


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
    def __init__(self, postgrest_url: str, token: str):
        self.http = httpx.Client(base_url=postgrest_url, timeout=30)
        self.token = token
        self.request_count = 0
        self.response_bytes: list[int] = []

    def rpc(self, name: str, params: dict):
        return _RpcExecution(self, name, params)

    def close(self):
        self.http.close()


@pytest.fixture
def rpc_client(postgrest_url, service_token):
    client = _CountingRpcClient(postgrest_url, service_token)
    yield client
    client.close()


def _raw_rpc(
    postgrest_url: str,
    token: str,
    name: str,
    payload: dict,
) -> httpx.Response:
    return httpx.post(
        f"{postgrest_url}/rpc/{name}",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )


def _position_count(postgrest_url: str, service_token: str) -> int:
    response = httpx.get(
        f"{postgrest_url}/order_events",
        headers=_headers(service_token, prefer="count=exact"),
        params={"select": "id", "limit": "1"},
        timeout=30,
    )
    response.raise_for_status()
    return int(response.headers["content-range"].rsplit("/", 1)[1])


def _traverse(
    client: _CountingRpcClient,
    subject: PositionSubject,
    stream: str,
) -> tuple[list[dict], int]:
    rows = []
    cursor = None
    request_start = client.request_count
    while True:
        payload = fetch_position_rpc(
            client,
            subject=subject,
            stream=stream,
            limit=100,
            cursor=cursor,
        )
        page = build_position_page(
            payload,
            subject=subject,
            stream=stream,
            limit=100,
            changed_after=None,
        )
        rows.extend(page["positions"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return rows, client.request_count - request_start


def test_fixture_has_exactly_100k_rows(postgrest_url, service_token):
    assert _position_count(postgrest_url, service_token) == 100_000


def test_snapshot_and_pages_are_bounded_by_cardinality_and_bytes(rpc_client):
    subject = PositionSubject(account_id=SCALE_ACCOUNT_ID)
    before = rpc_client.request_count
    payload = fetch_position_rpc(
        rpc_client,
        subject=subject,
        stream="snapshot",
    )

    assert rpc_client.request_count - before == 1
    assert len(payload["active"]) <= 51
    assert len(payload["settled"]) <= 21
    assert rpc_client.response_bytes[-1] < 256 * 1024

    snapshot = build_position_snapshot(payload, subject=subject)
    assert len(snapshot["positions"]) <= 70
    assert snapshot["active"]["has_more"] is True
    assert snapshot["settled"]["has_more"] is True

    payload = fetch_position_rpc(
        rpc_client,
        subject=subject,
        stream="active",
        limit=100,
    )
    assert len(payload["rows"]) <= 101
    assert rpc_client.response_bytes[-1] < 128 * 1024


@pytest.mark.parametrize(
    ("subject", "expected_wallets"),
    [
        (PositionSubject(account_id=SINGLE_ACCOUNT_ID), 1),
        (PositionSubject(account_id=SCALE_ACCOUNT_ID), 100),
        (PositionSubject(privy_user_id="did:privy:scale"), 100),
    ],
)
def test_identity_resolution_is_one_rpc_independent_of_wallet_count(
    rpc_client,
    subject,
    expected_wallets,
):
    before = rpc_client.request_count
    payload = fetch_position_rpc(
        rpc_client,
        subject=subject,
        stream="active",
        limit=1,
    )

    assert rpc_client.request_count - before == 1
    assert len(payload["rows"]) <= 2
    assert expected_wallets in {1, 100}


@pytest.mark.parametrize(
    ("stream", "expected"),
    [("active", 80_000), ("settled", 20_000)],
)
def test_every_100k_fixture_position_is_reachable_exactly_once(
    rpc_client,
    stream,
    expected,
):
    subject = PositionSubject(account_id=SCALE_ACCOUNT_ID)
    seen: set[str] = set()
    cursor = None
    request_start = rpc_client.request_count

    while True:
        payload = fetch_position_rpc(
            rpc_client,
            subject=subject,
            stream=stream,
            limit=100,
            cursor=cursor,
        )
        page = build_position_page(
            payload,
            subject=subject,
            stream=stream,
            limit=100,
            changed_after=None,
        )
        page_ids = {row["id"] for row in page["positions"]}
        assert len(page_ids) == len(page["positions"])
        assert seen.isdisjoint(page_ids)
        seen.update(page_ids)
        cursor = page["next_cursor"]
        if cursor is None:
            break

    query_count = rpc_client.request_count - request_start
    assert len(seen) == expected
    assert query_count == (expected + 99) // 100
    assert max(rpc_client.response_bytes[-query_count:]) < 128 * 1024


@pytest.mark.parametrize("stream", ["active", "settled"])
def test_tied_keyset_pages_reach_every_wallet_position_exactly_once(
    rpc_client,
    stream,
):
    subject = normalized_wallet_subject(
        [("base", TEST_WALLET)],
        scope="integration-wallet",
    )
    rows, query_count = _traverse(rpc_client, subject, stream)
    ids = [row["id"] for row in rows]

    expected = 248 if stream == "active" else 63
    assert len(rows) == expected
    assert len(ids) == len(set(ids))
    assert query_count == (expected + 99) // 100
    assert max(rpc_client.response_bytes[-query_count:]) < 128 * 1024
    assert all("settled_sort_at" not in row for row in rows)


def test_cursor_watermark_and_delta_capture_insert_and_settlement(
    rpc_client,
    postgrest_url,
    service_token,
):
    subject = normalized_wallet_subject(
        [("base", TEST_WALLET)],
        scope="integration-mutation",
    )
    initial_payload = fetch_position_rpc(
        rpc_client,
        subject=subject,
        stream="active",
        limit=10,
    )
    initial_page = build_position_page(
        initial_payload,
        subject=subject,
        stream="active",
        limit=10,
        changed_after=None,
    )
    initial_watermark = initial_page["watermark"]
    assert initial_watermark > WATERMARK_FLOOR

    target_id = initial_page["positions"][-1]["id"]
    new_id = "ffffffff-ffff-4fff-8fff-ffffffff0430"
    insert_response = httpx.post(
        f"{postgrest_url}/order_events",
        headers=_headers(service_token, prefer="return=representation"),
        json={
            "id": new_id,
            "tx_hash": "0x" + "f" * 60 + "0430",
            "block_number": 100_001,
            "log_index": 0,
            "chain": "base",
            "user_address": TEST_WALLET,
            "otoken_address": "0x0000000000000000000000000000000000000430",
            "amount": 100000000,
            "premium": 1000000,
            "collateral": 1000000,
            "vault_id": 100_001,
            "strike_price": 300000000000,
            "expiry": 2000000000,
            "is_put": False,
            "is_settled": False,
            "indexed_at": "2026-08-03T13:00:00Z",
            "asset": "eth",
        },
        timeout=30,
    )
    assert insert_response.status_code == 201, insert_response.text

    settle_response = httpx.patch(
        f"{postgrest_url}/order_events",
        headers=_headers(service_token, prefer="return=representation"),
        params={"id": f"eq.{target_id}"},
        json={
            "is_settled": True,
            "settled_at": "2026-08-03T13:00:01Z",
            "settlement_type": "otm",
        },
        timeout=30,
    )
    assert settle_response.status_code == 200, settle_response.text

    continuation = fetch_position_rpc(
        rpc_client,
        subject=subject,
        stream="active",
        limit=100,
        cursor=initial_page["next_cursor"],
    )
    continuation_ids = {row["id"] for row in continuation["rows"]}
    assert new_id not in continuation_ids
    assert target_id not in continuation_ids

    change_rows = []
    cursor = None
    while True:
        change_payload = fetch_position_rpc(
            rpc_client,
            subject=subject,
            stream="changes",
            limit=1,
            cursor=cursor,
            changed_after=initial_watermark,
        )
        change_page = build_position_page(
            change_payload,
            subject=subject,
            stream="changes",
            limit=1,
            changed_after=initial_watermark,
        )
        change_rows.extend(change_page["positions"])
        cursor = change_page["next_cursor"]
        if cursor is None:
            break

    assert {row["id"] for row in change_rows} == {new_id, target_id}
    assert len(change_rows) == 2


def _first_position_cursor(rpc_client, subject):
    payload = fetch_position_rpc(
        rpc_client,
        subject=subject,
        stream="active",
        limit=1,
    )
    return build_position_page(
        payload,
        subject=subject,
        stream="active",
        limit=1,
        changed_after=None,
    )["next_cursor"]


def test_account_cursor_rejects_verified_wallet_set_mutation(
    rpc_client,
    postgrest_url,
    service_token,
):
    subject = PositionSubject(account_id=SCALE_ACCOUNT_ID)
    cursor = _first_position_cursor(rpc_client, subject)
    changed_wallet = "0x0000000000000000000000000000000000000064"
    update_url = f"{postgrest_url}/b1nary_wallets"
    update_params = {"address_normalized": f"eq.{changed_wallet}"}

    changed = httpx.patch(
        update_url,
        headers=_headers(service_token, prefer="return=representation"),
        params=update_params,
        json={"role": "funding"},
        timeout=30,
    )
    assert changed.status_code == 200, changed.text
    try:
        with pytest.raises(PositionCursorError, match="wallet filter changed"):
            fetch_position_rpc(
                rpc_client,
                subject=subject,
                stream="active",
                limit=1,
                cursor=cursor,
            )
    finally:
        restored = httpx.patch(
            update_url,
            headers=_headers(service_token, prefer="return=representation"),
            params=update_params,
            json={"role": "trading"},
            timeout=30,
        )
        assert restored.status_code == 200, restored.text


def test_privy_cursor_rejects_membership_mutation(
    rpc_client,
    postgrest_url,
    service_token,
):
    subject = PositionSubject(privy_user_id="did:privy:scale")
    cursor = _first_position_cursor(rpc_client, subject)
    member_url = f"{postgrest_url}/b1nary_account_members"

    changed = httpx.patch(
        member_url,
        headers=_headers(service_token, prefer="return=representation"),
        params={"privy_user_id": "eq.did:privy:scale"},
        json={"privy_user_id": "did:privy:scale-changed"},
        timeout=30,
    )
    assert changed.status_code == 200, changed.text
    try:
        with pytest.raises(PositionCursorError, match="wallet filter changed"):
            fetch_position_rpc(
                rpc_client,
                subject=subject,
                stream="active",
                limit=1,
                cursor=cursor,
            )
    finally:
        restored = httpx.patch(
            member_url,
            headers=_headers(service_token, prefer="return=representation"),
            params={"privy_user_id": "eq.did:privy:scale-changed"},
            json={"privy_user_id": "did:privy:scale"},
            timeout=30,
        )
        assert restored.status_code == 200, restored.text


def test_price_count_rpc_matches_legacy_groups_and_bounds_output(
    postgrest_url,
    service_token,
):
    series = [
        {"strike_price": "3000", "is_put": False, "expiry": 2000000000},
        {"strike_price": "3000", "is_put": False, "expiry": 2000007200},
        {"strike_price": "3100", "is_put": True, "expiry": 2000000000},
    ]
    parameters = {"p_asset": "eth", "p_chain": "base", "p_now": 1900000000}
    legacy_response = _raw_rpc(
        postgrest_url,
        service_token,
        "b1n430_fixture_legacy_position_counts",
        parameters,
    )
    legacy_response.raise_for_status()
    bounded_rows = []
    bounded_bytes = 0
    for batch in _price_count_series_batches(series):
        bounded_response = _raw_rpc(
            postgrest_url,
            service_token,
            "b1nary_price_position_counts",
            {**parameters, "p_series": batch},
        )
        bounded_response.raise_for_status()
        bounded_rows.extend(bounded_response.json())
        bounded_bytes += len(bounded_response.content)

    expected = [0] * len(series)
    for source in legacy_response.json():
        candidates = [
            (index, item)
            for index, item in enumerate(series)
            if Decimal(item["strike_price"]) == Decimal(source["strike_price"])
            and item["is_put"] == source["is_put"]
        ]
        if not candidates:
            continue
        chosen_index = min(
            candidates,
            key=lambda candidate: (
                candidate[1]["expiry"] != source["expiry"],
                abs(candidate[1]["expiry"] - source["expiry"]),
                candidate[0],
            ),
        )[0]
        expected[chosen_index] += source["position_count"]

    bounded = bounded_rows
    actual = [0] * len(series)
    for row in bounded:
        index = series.index(
            {
                "strike_price": row["strike_price"],
                "is_put": row["is_put"],
                "expiry": row["expiry"],
            }
        )
        actual[index] = row["position_count"]

    assert actual == expected
    assert len(bounded) <= len(series)
    assert bounded_bytes < 16 * 1024


@pytest.mark.parametrize(
    ("rpc_name", "payload"),
    [
        (
            "b1nary_position_page",
            {
                "p_account_id": SCALE_ACCOUNT_ID,
                "p_stream": "active",
                "p_limit": 1,
            },
        ),
        (
            "b1nary_price_position_counts",
            {
                "p_asset": "eth",
                "p_chain": "base",
                "p_now": 1900000000,
                "p_series": [
                    {
                        "strike_price": "3000",
                        "is_put": False,
                        "expiry": 2000000000,
                        "lower_expiry": None,
                        "upper_expiry": None,
                    }
                ],
            },
        ),
    ],
)
def test_rpc_execute_is_restricted_to_service_role(
    postgrest_url,
    service_token,
    rpc_name,
    payload,
):
    anonymous = _required_env("B1N430_ANON_TOKEN")
    authenticated = _required_env("B1N430_AUTHENTICATED_TOKEN")

    anon_response = _raw_rpc(
        postgrest_url,
        anonymous,
        rpc_name,
        payload,
    )
    authenticated_response = _raw_rpc(
        postgrest_url,
        authenticated,
        rpc_name,
        payload,
    )
    service_response = _raw_rpc(
        postgrest_url,
        service_token,
        rpc_name,
        payload,
    )

    assert anon_response.status_code == 401
    assert authenticated_response.status_code == 403
    assert service_response.status_code == 200
