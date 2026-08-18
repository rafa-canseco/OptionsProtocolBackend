from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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


def _claims(**overrides) -> dict:
    claims = {
        "sub": USER.user_id,
        "sid": USER.session_id,
        "aud": "app-id",
        "iss": "privy.io",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    claims.update(overrides)
    return claims


def _jwk(private_key, kid: str) -> dict:
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(
        private_key.public_key(),
        as_dict=True,
    )
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return jwk


def test_privy_es256_access_token_is_verified(monkeypatch) -> None:
    private_key, public_key = _jwt_material()
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", public_key)
    monkeypatch.setattr(
        settings,
        "privy_jwks_url",
        "https://auth.example/.well-known/jwks.json",
    )
    token = jwt.encode(
        _claims(),
        private_key,
        algorithm="ES256",
    )

    with patch("src.api.user_auth._get_privy_jwks_client") as get_jwks_client:
        user = require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )

    assert user == USER
    get_jwks_client.assert_not_called()


def test_privy_jwks_selects_signing_key_by_kid_and_caches(monkeypatch) -> None:
    wrong_key = ec.generate_private_key(ec.SECP256R1())
    signing_key = ec.generate_private_key(ec.SECP256R1())
    jwks_client = jwt.PyJWKClient(
        "https://auth.example/.well-known/jwks.json",
        cache_keys=True,
    )
    jwks_client.fetch_data = MagicMock(
        return_value={
            "keys": [
                _jwk(wrong_key, "rotated-out"),
                _jwk(signing_key, "active"),
            ]
        }
    )
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "")
    monkeypatch.setattr(
        settings,
        "privy_jwks_url",
        "https://auth.example/.well-known/jwks.json",
    )
    token = jwt.encode(
        _claims(),
        signing_key,
        algorithm="ES256",
        headers={"kid": "active"},
    )

    with patch(
        "src.api.user_auth._get_privy_jwks_client",
        return_value=jwks_client,
    ):
        first = require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )
        second = require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )

    assert first == USER
    assert second == USER
    assert jwks_client.fetch_data.call_count == 1


def test_privy_token_with_wrong_audience_is_rejected(monkeypatch) -> None:
    private_key, public_key = _jwt_material()
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", public_key)
    monkeypatch.setattr(settings, "privy_jwks_url", "")
    token = jwt.encode(
        _claims(aud="another-app"),
        private_key,
        algorithm="ES256",
    )

    with pytest.raises(HTTPException) as raised:
        require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )

    assert raised.value.detail["code"] == "AUTH_TOKEN_INVALID"


def test_privy_token_with_wrong_issuer_is_rejected(monkeypatch) -> None:
    private_key, public_key = _jwt_material()
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", public_key)
    monkeypatch.setattr(settings, "privy_jwks_url", "")
    token = jwt.encode(
        _claims(iss="https://issuer.example"),
        private_key,
        algorithm="ES256",
    )

    with pytest.raises(HTTPException) as raised:
        require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )

    assert raised.value.detail["code"] == "AUTH_TOKEN_INVALID"


def test_privy_auth_is_unavailable_without_pem_or_jwks(monkeypatch) -> None:
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "")
    monkeypatch.setattr(settings, "privy_jwks_url", "")

    with pytest.raises(HTTPException) as raised:
        require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")
        )

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "AUTH_UNAVAILABLE"


def test_privy_jwks_failure_does_not_log_url_or_token(monkeypatch, caplog) -> None:
    jwks_url = "https://auth.example/.well-known/jwks.json"
    token = "not-a-real-token"
    jwks_client = MagicMock()
    jwks_client.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientConnectionError(
        f"failed to fetch {jwks_url} for {token}"
    )
    monkeypatch.setattr(settings, "privy_app_id", "app-id")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "")
    monkeypatch.setattr(settings, "privy_jwks_url", jwks_url)

    with (
        patch(
            "src.api.user_auth._get_privy_jwks_client",
            return_value=jwks_client,
        ),
        pytest.raises(HTTPException) as raised,
    ):
        require_privy_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )

    assert raised.value.status_code == 503
    assert jwks_url not in caplog.text
    assert token not in caplog.text
