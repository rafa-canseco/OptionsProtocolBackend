import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import src.api.mm_routes as mm_routes
import src.contracts.web3_client as web3_client
from src.api.deps import require_mm_api_key
from src.config import settings
from src.main import app


ENVIRONMENT = "quote-test"
CHAIN_ID = 84532


def _snapshot(**updates):
    payload = json.loads(Path("tests/fixtures/rpc_snapshot_envelope.json").read_text())
    payload.update(environment=ENVIRONMENT, chain_id=CHAIN_ID)
    payload.update(updates)
    return payload


def _set(payload: dict, path: tuple[str | int, ...], value):
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return payload


def _quote(maker_nonce: int) -> dict:
    return {
        "quotes": [
            {
                "otoken_address": "0x" + "bb" * 20,
                "bid_price": 1_000_000,
                "deadline": 2_000_000_000,
                "quote_id": 7,
                "max_amount": 100_000_000,
                "maker_nonce": maker_nonce,
                "signature": "0x" + "cc" * 65,
                "chain": "base",
                "asset": "eth",
                "strike_price": 2_500,
                "expiry": 2_000_100_000,
                "is_put": True,
            }
        ]
    }


def _forbid_provider(*_args, **_kwargs):
    raise AssertionError("Base quote submission must not call a Web3 provider")


@pytest.fixture
def base_quote_endpoint(monkeypatch):
    payload = _snapshot()
    mm_address = payload["common"]["market_maker"]["mm_address"]
    database = MagicMock()
    database.rpc.return_value.execute.return_value.data = payload
    monkeypatch.setattr(settings, "app_env", ENVIRONMENT)
    monkeypatch.setattr(settings, "chain_id", CHAIN_ID)
    monkeypatch.setattr(mm_routes, "get_client", lambda: database)
    monkeypatch.setattr(mm_routes, "get_w3", _forbid_provider)
    monkeypatch.setattr(web3_client, "get_batch_settler", _forbid_provider)
    monkeypatch.setattr(mm_routes, "recover_quote_signer", lambda **_kwargs: mm_address)
    app.dependency_overrides[require_mm_api_key] = lambda: mm_address
    try:
        yield database, payload, mm_address
    finally:
        app.dependency_overrides.pop(require_mm_api_key, None)


def test_base_quote_uses_one_atomic_snapshot_and_zero_provider_calls(
    base_quote_endpoint,
) -> None:
    database, payload, _mm_address = base_quote_endpoint
    nonce = payload["common"]["market_maker"]["maker_nonce"]

    response = TestClient(app).post("/mm/quotes", json=_quote(nonce))

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1
    database.rpc.assert_called_once_with(
        "v2_get_current_snapshot",
        {"p_environment": ENVIRONMENT, "p_chain_id": CHAIN_ID},
    )
    assert not database.table.return_value.select.called


@pytest.mark.parametrize(
    ("mutation", "status"),
    [
        (lambda _payload: None, 503),
        (lambda payload: {**payload, "stale": True}, 503),
        (lambda payload: {**payload, "reconciled": False}, 503),
        (
            lambda payload: {
                **payload,
                "common": {
                    **payload["common"],
                    "market_maker": {
                        **payload["common"]["market_maker"],
                        "mm_address": "0x" + "99" * 20,
                    },
                },
            },
            403,
        ),
    ],
)
def test_base_quote_rejects_absent_stale_or_wrong_snapshot_signer(
    base_quote_endpoint, mutation, status
) -> None:
    database, payload, _mm_address = base_quote_endpoint
    database.rpc.return_value.execute.return_value.data = mutation(payload)

    response = TestClient(app).post("/mm/quotes", json=_quote(7))

    assert response.status_code == status
    database.rpc.assert_called_once()
    database.table.assert_not_called()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("common", "market_maker", "maker_nonce"), True),
        (("common", "market_maker", "maker_nonce"), "7"),
        (("generation",), "42"),
        (("snapshot_block_hash",), "0x01"),
        (
            (
                "funds",
                2,
                "state",
                "allocator",
                "wheel_snapshot",
                "protocol_premium_fee_bps",
            ),
            "malformed",
        ),
        (
            (
                "funds",
                2,
                "state",
                "allocator",
                "wheel_snapshot",
                "parent_total_assets_usdc",
            ),
            "malformed",
        ),
    ],
)
def test_base_quote_rejects_malformed_atomic_snapshot_before_validation_or_write(
    base_quote_endpoint, path, value
) -> None:
    database, payload, _mm_address = base_quote_endpoint
    database.rpc.return_value.execute.return_value.data = _set(payload, path, value)

    response = TestClient(app).post("/mm/quotes", json=_quote(7))

    assert response.status_code == 503
    database.rpc.assert_called_once()
    database.table.assert_not_called()


def test_base_quote_rejects_snapshot_nonce_mismatch_without_provider_or_write(
    base_quote_endpoint,
) -> None:
    database, payload, _mm_address = base_quote_endpoint
    nonce = payload["common"]["market_maker"]["maker_nonce"]

    response = TestClient(app).post("/mm/quotes", json=_quote(nonce + 1))

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert response.json()["rejected"] == 1
    assert "current snapshot" in response.json()["errors"][0]
    database.rpc.assert_called_once()
    database.table.assert_not_called()


def test_base_quote_rejects_signature_not_bound_to_authenticated_snapshot_signer(
    base_quote_endpoint, monkeypatch
) -> None:
    database, payload, _mm_address = base_quote_endpoint
    nonce = payload["common"]["market_maker"]["maker_nonce"]
    monkeypatch.setattr(
        mm_routes, "recover_quote_signer", lambda **_kwargs: "0x" + "88" * 20
    )

    response = TestClient(app).post("/mm/quotes", json=_quote(nonce))

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert "authenticated MM address" in response.json()["errors"][0]
    database.rpc.assert_called_once()
    database.table.assert_not_called()
