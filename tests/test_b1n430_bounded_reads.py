import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import src.api.routes as routes_module
from src.api.position_pagination import (
    LEGACY_ACTIVE_LIMIT,
    LEGACY_SETTLED_LIMIT,
    PositionCursorError,
    PositionSubject,
    build_position_page,
    build_position_snapshot,
    decode_position_cursor,
    encode_position_cursor,
    fetch_position_rpc,
    normalized_wallet_subject,
)
from src.main import app


WATERMARK = "2026-08-03T12:00:00Z"
WALLET_FINGERPRINT = "a" * 64


@pytest.fixture(autouse=True)
def synthetic_cursor_secret(monkeypatch):
    monkeypatch.setattr(
        "src.api.position_pagination.settings.position_cursor_secret",
        "unit-only-b1n430-cursor-secret-at-least-32-bytes",
    )


class _RpcCall:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


class _RpcClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _RpcCall(self.data)


def _row(index: int, *, settled: bool = False):
    timestamp = f"2026-08-03T11:{index % 60:02d}:00Z"
    return {
        "id": str(uuid4()),
        "indexed_at": timestamp,
        "updated_at": timestamp,
        "settled_at": timestamp if settled else None,
        "is_settled": settled,
    }


def test_cursor_is_opaque_integrity_protected_and_filter_bound():
    subject = normalized_wallet_subject(
        [("base", "0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD")],
        scope="wallet-route",
    )
    key_id = str(uuid4())
    cursor = encode_position_cursor(
        subject=subject,
        stream="active",
        changed_after=None,
        key_at="2026-08-03T11:00:00+00:00",
        key_id=key_id,
        watermark=WATERMARK,
        wallet_fingerprint=WALLET_FINGERPRINT,
    )

    assert cursor.startswith("v1.")
    assert key_id not in cursor
    assert decode_position_cursor(
        cursor,
        subject=subject,
        stream="active",
        changed_after=None,
    ) == {
        "key_at": "2026-08-03T11:00:00Z",
        "key_id": key_id,
        "watermark": WATERMARK,
        "wallet_fingerprint": WALLET_FINGERPRINT,
    }

    prefix, payload, signature = cursor.split(".")
    midpoint = len(signature) // 2
    replacement = "A" if signature[midpoint] != "A" else "B"
    tampered = f"{prefix}.{payload}.{signature[:midpoint]}{replacement}{signature[midpoint + 1 :]}"
    with pytest.raises(PositionCursorError):
        decode_position_cursor(
            tampered,
            subject=subject,
            stream="active",
            changed_after=None,
        )

    with pytest.raises(PositionCursorError):
        decode_position_cursor(
            cursor,
            subject=subject,
            stream="settled",
            changed_after=None,
        )

    other_subject = PositionSubject(account_id=str(uuid4()))
    with pytest.raises(PositionCursorError):
        decode_position_cursor(
            cursor,
            subject=other_subject,
            stream="active",
            changed_after=None,
        )


def test_cursor_rejects_invalid_key_fields():
    subject = PositionSubject(account_id=str(uuid4()))
    with pytest.raises(PositionCursorError, match="cursor key"):
        encode_position_cursor(
            subject=subject,
            stream="active",
            changed_after=None,
            key_at="not-a-timestamp",
            key_id=str(uuid4()),
            watermark=WATERMARK,
            wallet_fingerprint=WALLET_FINGERPRINT,
        )
    with pytest.raises(PositionCursorError, match="cursor key"):
        encode_position_cursor(
            subject=subject,
            stream="active",
            changed_after=None,
            key_at=WATERMARK,
            key_id="not-a-uuid",
            watermark=WATERMARK,
            wallet_fingerprint=WALLET_FINGERPRINT,
        )


def test_cursor_secret_must_be_dedicated_and_at_least_32_bytes(monkeypatch):
    anon_key = "anon-key-that-is-definitely-longer-than-32-bytes"
    service_key = "service-key-that-is-definitely-longer-than-32-bytes"
    monkeypatch.setattr(
        "src.api.position_pagination.settings.supabase_anon_key", anon_key
    )
    monkeypatch.setattr(
        "src.api.position_pagination.settings.supabase_service_role_key",
        service_key,
    )
    subject = PositionSubject(account_id=str(uuid4()))

    for invalid in ("", "too-short", anon_key, service_key):
        monkeypatch.setattr(
            "src.api.position_pagination.settings.position_cursor_secret", invalid
        )
        with pytest.raises(RuntimeError, match="signing key"):
            encode_position_cursor(
                subject=subject,
                stream="active",
                changed_after=None,
                key_at=WATERMARK,
                key_id=str(uuid4()),
                watermark=WATERMARK,
                wallet_fingerprint=WALLET_FINGERPRINT,
            )

    valid_secret = secrets.token_urlsafe(32)
    monkeypatch.setattr(
        "src.api.position_pagination.settings.position_cursor_secret", valid_secret
    )
    cursor = encode_position_cursor(
        subject=subject,
        stream="active",
        changed_after=None,
        key_at=WATERMARK,
        key_id=str(uuid4()),
        watermark=WATERMARK,
        wallet_fingerprint=WALLET_FINGERPRINT,
    )
    assert (
        decode_position_cursor(
            cursor,
            subject=subject,
            stream="active",
            changed_after=None,
        )["wallet_fingerprint"]
        == WALLET_FINGERPRINT
    )


@pytest.mark.parametrize("invalid_kind", ["missing", "short", "anon", "service"])
def test_invalid_cursor_secret_returns_503_on_continuation(
    monkeypatch,
    invalid_kind,
):
    anon_key = "anon-key-that-is-definitely-longer-than-32-bytes"
    service_key = "service-key-that-is-definitely-longer-than-32-bytes"
    invalid_values = {
        "missing": "",
        "short": "too-short",
        "anon": anon_key,
        "service": service_key,
    }
    monkeypatch.setattr(
        "src.api.position_pagination.settings.supabase_anon_key", anon_key
    )
    monkeypatch.setattr(
        "src.api.position_pagination.settings.supabase_service_role_key",
        service_key,
    )
    monkeypatch.setattr(
        "src.api.position_pagination.settings.position_cursor_secret",
        invalid_values[invalid_kind],
    )
    rpc_client = _RpcClient(
        {
            "active": [_row(index) for index in range(LEGACY_ACTIVE_LIMIT + 1)],
            "settled": [],
            "rows": [],
            "watermark": WATERMARK,
            "wallet_fingerprint": WALLET_FINGERPRINT,
        }
    )
    monkeypatch.setattr(routes_module, "get_client", lambda: rpc_client)
    routes_module._read_hits.clear()

    response = TestClient(app).get(
        "/positions/0x0000000000000000000000000000000000000001"
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Position pagination is unavailable"}


def test_missing_signing_key_fails_only_when_continuation_is_needed(monkeypatch):
    monkeypatch.setattr(
        "src.api.position_pagination.settings.position_cursor_secret", ""
    )
    subject = PositionSubject(account_id=str(uuid4()))

    bounded = build_position_page(
        {
            "rows": [_row(1)],
            "watermark": WATERMARK,
            "wallet_fingerprint": WALLET_FINGERPRINT,
        },
        subject=subject,
        stream="active",
        limit=1,
        changed_after=None,
    )
    assert bounded["next_cursor"] is None

    with pytest.raises(RuntimeError, match="signing key"):
        build_position_page(
            {
                "rows": [_row(1), _row(2)],
                "watermark": WATERMARK,
                "wallet_fingerprint": WALLET_FINGERPRINT,
            },
            subject=subject,
            stream="active",
            limit=1,
            changed_after=None,
        )


def test_snapshot_preserves_legacy_shape_without_silent_truncation():
    subject = PositionSubject(account_id=str(uuid4()))
    payload = {
        "active": [_row(index) for index in range(LEGACY_ACTIVE_LIMIT + 1)],
        "settled": [
            _row(index, settled=True) for index in range(LEGACY_SETTLED_LIMIT + 1)
        ],
        "watermark": WATERMARK,
        "wallet_fingerprint": WALLET_FINGERPRINT,
    }

    snapshot = build_position_snapshot(payload, subject=subject)

    assert len(snapshot["positions"]) == LEGACY_ACTIVE_LIMIT + LEGACY_SETTLED_LIMIT
    assert snapshot["bounded"] is True
    assert snapshot["active"]["has_more"] is True
    assert snapshot["active"]["next_cursor"]
    assert snapshot["settled"]["has_more"] is True
    assert snapshot["settled"]["next_cursor"]
    expected_order = sorted(
        snapshot["positions"],
        key=lambda row: (row["indexed_at"], row["id"]),
        reverse=True,
    )
    assert snapshot["positions"] == expected_order


def test_wallet_route_keeps_legacy_list_and_exposes_traversal_headers(monkeypatch):
    payload = {
        "active": [_row(index) for index in range(LEGACY_ACTIVE_LIMIT + 1)],
        "settled": [
            _row(index, settled=True) for index in range(LEGACY_SETTLED_LIMIT + 1)
        ],
        "rows": [_row(1), _row(2)],
        "watermark": WATERMARK,
        "wallet_fingerprint": WALLET_FINGERPRINT,
    }
    rpc_client = _RpcClient(payload)
    monkeypatch.setattr(routes_module, "get_client", lambda: rpc_client)
    routes_module._read_hits.clear()
    api_client = TestClient(app)

    legacy = api_client.get("/positions/0x0000000000000000000000000000000000000001")
    page = api_client.get(
        "/positions/0x0000000000000000000000000000000000000001?stream=active&limit=1"
    )

    assert legacy.status_code == 200
    assert isinstance(legacy.json(), list)
    assert len(legacy.json()) == LEGACY_ACTIVE_LIMIT + LEGACY_SETTLED_LIMIT
    assert legacy.headers["x-portfolio-bounded"] == "true"
    assert legacy.headers["x-portfolio-watermark"] == WATERMARK
    assert legacy.headers["x-active-has-more"] == "true"
    assert legacy.headers["x-active-next-cursor"].startswith("v1.")
    assert legacy.headers["x-settled-has-more"] == "true"
    assert legacy.headers["x-settled-next-cursor"].startswith("v1.")

    assert page.status_code == 200
    assert page.json()["stream"] == "active"
    assert page.json()["has_more"] is True
    assert len(page.json()["positions"]) == 1
    assert len(rpc_client.calls) == 2


def test_batch_wallet_route_normalizes_deduplicates_and_uses_one_rpc(monkeypatch):
    rpc_client = _RpcClient(
        {
            "active": [],
            "settled": [],
            "rows": [],
            "watermark": WATERMARK,
            "wallet_fingerprint": WALLET_FINGERPRINT,
        }
    )
    monkeypatch.setattr(routes_module, "get_client", lambda: rpc_client)
    routes_module._read_hits.clear()
    api_client = TestClient(app)
    base = "0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD"
    solana = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"

    response = api_client.post(
        "/positions/batch",
        json={
            "wallets": [
                {"chain": "base", "address": base},
                {"chain": "base", "address": base.lower()},
                {"chain": "solana", "address": solana},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["pagination"]["bounded"] is True
    assert len(rpc_client.calls) == 1
    assert rpc_client.calls[0][1]["p_wallets"] == [
        {"chain": "base", "address": base.lower()},
        {"chain": "solana", "address": solana},
    ]


def test_batch_wallet_route_rejects_more_than_100_inputs(monkeypatch):
    rpc_client = _RpcClient({})
    monkeypatch.setattr(routes_module, "get_client", lambda: rpc_client)
    api_client = TestClient(app)

    response = api_client.post(
        "/positions/batch",
        json={
            "wallets": [
                {"chain": "base", "address": f"0x{index:040x}"}
                for index in range(1, 102)
            ]
        },
    )

    assert response.status_code == 422
    assert rpc_client.calls == []


def test_rpc_is_one_call_for_many_wallets_and_forwards_cursor_watermark():
    wallets = [("base", f"0x{index:040x}") for index in range(1, 51)]
    subject = normalized_wallet_subject(wallets, scope="privy:test-user")
    first_rows = [_row(1), _row(2)]
    client = _RpcClient(
        {
            "rows": first_rows,
            "watermark": WATERMARK,
            "wallet_fingerprint": WALLET_FINGERPRINT,
        }
    )

    first_payload = fetch_position_rpc(
        client,
        subject=subject,
        stream="active",
        limit=1,
    )
    page = build_position_page(
        first_payload,
        subject=subject,
        stream="active",
        limit=1,
        changed_after=None,
    )
    assert len(client.calls) == 1
    assert len(client.calls[0][1]["p_wallets"]) == 50

    second_client = _RpcClient(
        {
            "rows": [],
            "watermark": WATERMARK,
            "wallet_fingerprint": WALLET_FINGERPRINT,
        }
    )
    fetch_position_rpc(
        second_client,
        subject=subject,
        stream="active",
        limit=1,
        cursor=page["next_cursor"],
    )
    params = second_client.calls[0][1]
    assert params["p_cursor_id"] == first_rows[0]["id"]
    assert params["p_cursor_at"] == first_rows[0]["indexed_at"]
    assert params["p_watermark"] == WATERMARK


def test_changes_requires_and_binds_changed_after():
    subject = PositionSubject(privy_user_id="did:privy:test")
    client = _RpcClient(
        {
            "rows": [],
            "watermark": WATERMARK,
            "wallet_fingerprint": WALLET_FINGERPRINT,
        }
    )

    with pytest.raises(PositionCursorError, match="changed_after is required"):
        fetch_position_rpc(client, subject=subject, stream="changes")

    changed_after = "2026-08-03T10:00:00Z"
    payload = {
        "rows": [_row(1), _row(2)],
        "watermark": WATERMARK,
        "wallet_fingerprint": WALLET_FINGERPRINT,
    }
    client = _RpcClient(payload)
    response = fetch_position_rpc(
        client,
        subject=subject,
        stream="changes",
        limit=1,
        changed_after=changed_after,
    )
    page = build_position_page(
        response,
        subject=subject,
        stream="changes",
        limit=1,
        changed_after=changed_after,
    )

    with pytest.raises(PositionCursorError):
        fetch_position_rpc(
            _RpcClient(
                {
                    "rows": [],
                    "watermark": WATERMARK,
                    "wallet_fingerprint": WALLET_FINGERPRINT,
                }
            ),
            subject=subject,
            stream="changes",
            limit=1,
            changed_after="2026-08-03T09:00:00Z",
            cursor=page["next_cursor"],
        )


def test_continuation_rejects_changed_resolved_wallet_fingerprint():
    subject = PositionSubject(account_id=str(uuid4()))
    first_rows = [_row(1), _row(2)]
    first_client = _RpcClient(
        {
            "rows": first_rows,
            "watermark": WATERMARK,
            "wallet_fingerprint": WALLET_FINGERPRINT,
        }
    )
    first_payload = fetch_position_rpc(
        first_client,
        subject=subject,
        stream="active",
        limit=1,
    )
    cursor = build_position_page(
        first_payload,
        subject=subject,
        stream="active",
        limit=1,
        changed_after=None,
    )["next_cursor"]

    changed_client = _RpcClient(
        {
            "filter_mismatch": True,
            "rows": [],
            "watermark": WATERMARK,
            "wallet_fingerprint": "b" * 64,
        }
    )
    with pytest.raises(PositionCursorError, match="wallet filter changed"):
        fetch_position_rpc(
            changed_client,
            subject=subject,
            stream="active",
            limit=1,
            cursor=cursor,
        )
    assert changed_client.calls[0][1]["p_wallet_fingerprint"] == WALLET_FINGERPRINT


def test_migration_declares_bounded_projection_indexes_and_grants():
    migration = (
        Path(__file__).parents[1]
        / "supabase/migrations/202608030002_b1n430_bounded_reads.sql"
    ).read_text()
    lowered = migration.lower()

    assert "select *" not in lowered
    assert "limit p_limit + 1" in lowered
    assert "limit p_active_limit + 1" in lowered
    assert "limit p_settled_limit + 1" in lowered
    assert "security invoker" in lowered
    assert "set search_path = public, pg_temp" in lowered
    assert "from public, anon, authenticated" in lowered
    assert "to service_role" in lowered
    assert "idx_b1n430_order_events_active_page" in lowered
    assert "idx_b1n430_order_events_settled_page" in lowered
    assert "idx_b1n430_order_events_changes" in lowered
    assert "idx_b1n430_order_events_active_counts" in lowered
    assert "jsonb_array_length(p_series) > 100" in lowered
    assert "to_jsonb(page_row) - 'settled_sort_at'" in lowered


def test_cursor_payload_is_not_plain_json():
    subject = PositionSubject(privy_user_id="did:privy:test")
    cursor = encode_position_cursor(
        subject=subject,
        stream="active",
        changed_after=None,
        key_at=WATERMARK,
        key_id=str(uuid4()),
        watermark=WATERMARK,
        wallet_fingerprint=WALLET_FINGERPRINT,
    )

    with pytest.raises(json.JSONDecodeError):
        json.loads(cursor)
