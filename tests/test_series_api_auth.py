from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from src.api.series import router
from src.api.user_auth import AuthenticatedPrivyUser, require_privy_user
from src.config import settings
from src.otokens.service import EnsureResult

OTOKEN = "0x2222222222222222222222222222222222222222"
MM = "0x3333333333333333333333333333333333333333"
WALLET = "0x4444444444444444444444444444444444444444"
USER = AuthenticatedPrivyUser("did:privy:user", "session-id")


def _payload() -> dict:
    quote = {
        "otoken_address": OTOKEN,
        "bid_price_raw": "1000000",
        "deadline": "2000000000",
        "quote_id": "42",
        "max_amount_raw": "100000000",
        "maker_nonce": "7",
        "signature": "0x" + "ab" * 65,
        "mm_address": MM,
    }
    return {
        "wallet_address": WALLET,
        "expected_otoken_address": OTOKEN,
        "amount_raw": "1000000",
        "quote": quote,
    }


def _client() -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_privy_user] = lambda: USER
    return app, TestClient(app)


def test_ensure_endpoint_returns_exact_execution_quote() -> None:
    app, client = _client()
    payload = _payload()

    class FakeService:
        def ensure(self, body, user_id):
            assert user_id == USER.user_id
            result = EnsureResult(
                status="ready",
                otoken_address=OTOKEN,
                execution_quote=body.quote,
                deployment_tx_hash="0xcreate",
            )
            return result

    with (
        patch(
            "src.api.series.wallet_belongs_to_user",
            new=AsyncMock(return_value=True),
        ),
        patch("src.api.series.SeriesMaterializationService", FakeService),
    ):
        response = client.post("/series/ensure", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "otoken_address": OTOKEN,
        "execution_quote": payload["quote"],
        "retry_after_ms": None,
        "deployment_tx_hash": "0xcreate",
    }
    app.dependency_overrides.clear()


def test_ensure_endpoint_rejects_unlinked_wallet() -> None:
    app, client = _client()
    with patch(
        "src.api.series.wallet_belongs_to_user",
        new=AsyncMock(return_value=False),
    ):
        response = client.post("/series/ensure", json=_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "WALLET_NOT_AUTHORIZED",
        "message": "The execution wallet is not linked to this Privy session",
        "retryable": False,
    }
    app.dependency_overrides.clear()


def _jwt_material() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def test_privy_es256_access_token_is_verified(monkeypatch) -> None:
    private_key, public_key = _jwt_material()
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", public_key)
    token = jwt.encode(
        {
            "sub": USER.user_id,
            "sid": USER.session_id,
            "aud": "app-id",
            "iss": "privy.io",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        private_key,
        algorithm="ES256",
    )

    user = require_privy_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )

    assert user == USER


def test_privy_token_with_wrong_audience_is_rejected(monkeypatch) -> None:
    private_key, public_key = _jwt_material()
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", public_key)
    token = jwt.encode(
        {
            "sub": USER.user_id,
            "sid": USER.session_id,
            "aud": "another-app",
            "iss": "privy.io",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        private_key,
        algorithm="ES256",
    )

    with pytest.raises(HTTPException) as raised:
        require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )

    assert raised.value.detail["code"] == "AUTH_TOKEN_INVALID"
