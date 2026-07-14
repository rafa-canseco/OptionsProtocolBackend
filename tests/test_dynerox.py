import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def _mock_response(status_code: int, payload: dict):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


def test_dynerox_onramp_creates_user_then_base_usdc_route(monkeypatch):
    monkeypatch.setattr("src.api.dynerox.settings.dynerox_api_key", "sk_test_key")
    monkeypatch.setattr("src.api.dynerox.settings.dynerox_api_url", "https://dynerox.test")
    monkeypatch.setattr("src.api.dynerox.settings.dynerox_onramp_network", "base")
    monkeypatch.setattr("src.api.dynerox.settings.dynerox_onramp_currency", "USDC")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        side_effect=[
            _mock_response(201, {"user_id": "user-1"}),
            _mock_response(
                200,
                {
                    "route_id": "route-1",
                    "status": "pending_authorization",
                    "authorization_url": "https://stage-kyc.dynerox.com/auth/route-1",
                    "from": {"currency": {"symbol": "MXN"}, "network": {"name": "SPEI"}},
                    "to": {"account": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"},
                },
            ),
        ]
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.api.dynerox.httpx.AsyncClient", return_value=mock_client):
        response = client.post(
            "/api/dynerox/onramp",
            json={
                "first_name": "Juan",
                "last_name": "Garcia",
                "email": "juan@example.com",
                "curp": "GALJ900101HMCRPN09",
                "wallet_address": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "user-1"
    assert body["route_id"] == "route-1"
    assert body["authorization_url"].startswith("https://stage-kyc.dynerox.com")

    user_call = mock_client.post.call_args_list[0]
    route_call = mock_client.post.call_args_list[1]
    assert user_call.kwargs["headers"]["x-api-key"] == "sk_test_key"
    assert user_call.args[0] == "https://dynerox.test/v1/public/users"
    assert route_call.args[0] == "https://dynerox.test/v1/public/routes"
    assert route_call.kwargs["json"] == {
        "user_id": "user-1",
        "from": {"currency": "MXN", "network": "SPEI"},
        "to": {
            "currency": "USDC",
            "network": "base",
            "account": "0x742D35CC6634C0532925a3B844Bc9E7595F2bD18",
        },
    }


def test_dynerox_onramp_requires_api_key(monkeypatch):
    monkeypatch.setattr("src.api.dynerox.settings.dynerox_api_key", "")
    response = client.post(
        "/api/dynerox/onramp",
        json={
            "first_name": "Juan",
            "last_name": "Garcia",
            "email": "juan@example.com",
            "curp": "GALJ900101HMCRPN09",
            "wallet_address": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
        },
    )

    assert response.status_code == 503


def test_dynerox_webhook_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr("src.api.dynerox.settings.dynerox_webhook_secret", "whsec_test")
    payload = b'{"event":"route.activated","data":{"route_id":"route-1"}}'
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"whsec_test",
        timestamp.encode() + b"." + payload,
        hashlib.sha256,
    ).hexdigest()

    response = client.post(
        "/api/dynerox/webhook",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-dynerox-timestamp": timestamp,
            "x-dynerox-signature": signature,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"received": True}


def test_dynerox_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr("src.api.dynerox.settings.dynerox_webhook_secret", "whsec_test")
    response = client.post(
        "/api/dynerox/webhook",
        json={"event": "route.activated"},
        headers={
            "x-dynerox-timestamp": str(int(time.time())),
            "x-dynerox-signature": "bad",
        },
    )

    assert response.status_code == 401
