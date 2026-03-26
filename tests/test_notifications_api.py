from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

VALID_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
VALID_EMAIL = "user@example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear notification rate-limit state between tests."""
    from src.api import notifications as mod

    mod._wallet_hits.clear()
    mod._ip_hits.clear()
    yield
    mod._wallet_hits.clear()
    mod._ip_hits.clear()


def _mock_client_with_data(data):
    """Return a mock Supabase client returning data for any query chain."""
    mock = MagicMock()
    mock_result = MagicMock()
    mock_result.data = data
    # Support arbitrary chaining
    chain = mock.table.return_value
    for method in [
        "select",
        "eq",
        "is_",
        "upsert",
        "update",
        "insert",
    ]:
        sub = getattr(chain, method).return_value
        sub.execute.return_value = mock_result
        sub.eq.return_value = sub
        sub.is_.return_value = sub
    return mock


# --- POST /notifications/email ---


def test_submit_email_success():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email") as mock_send,
    ):
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_send.assert_called_once()


def test_submit_email_invalid_wallet():
    resp = client.post(
        "/notifications/email",
        json={"wallet_address": "bad", "email": VALID_EMAIL},
    )
    assert resp.status_code == 422


def test_submit_email_invalid_email():
    resp = client.post(
        "/notifications/email",
        json={"wallet_address": VALID_WALLET, "email": "not-an-email"},
    )
    assert resp.status_code == 422


def test_submit_email_rate_limit_by_wallet():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email"),
    ):
        for _ in range(3):
            resp = client.post(
                "/notifications/email",
                json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
            )
            assert resp.status_code == 200
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
        )
    assert resp.status_code == 429


def test_submit_email_clears_verified_on_change():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email"),
    ):
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": "new@example.com"},
        )
    assert resp.status_code == 200
    upsert_call = mock_db.table.return_value.upsert
    upsert_call.assert_called_once()
    upsert_data = upsert_call.call_args[0][0]
    assert upsert_data["verified_at"] is None


def test_submit_email_clears_unsubscribed():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email"),
    ):
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
        )
    assert resp.status_code == 200
    upsert_data = mock_db.table.return_value.upsert.call_args[0][0]
    assert upsert_data["unsubscribed_at"] is None


# --- POST /notifications/verify ---


def test_verify_success():
    now = datetime.now(timezone.utc)
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "verification_code": "123456",
        "code_expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.post(
            "/notifications/verify",
            json={"wallet_address": VALID_WALLET, "code": "123456"},
        )
    assert resp.status_code == 200
    assert resp.json()["verified"] is True


def test_verify_wrong_code():
    now = datetime.now(timezone.utc)
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "verification_code": "123456",
        "code_expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.post(
            "/notifications/verify",
            json={"wallet_address": VALID_WALLET, "code": "000000"},
        )
    assert resp.status_code == 400


def test_verify_expired_code():
    past = datetime.now(timezone.utc) - timedelta(minutes=15)
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "verification_code": "123456",
        "code_expires_at": past.isoformat(),
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.post(
            "/notifications/verify",
            json={"wallet_address": VALID_WALLET, "code": "123456"},
        )
    assert resp.status_code == 400


# --- GET /notifications/status ---


def test_status_verified_user():
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "email": VALID_EMAIL,
        "verified_at": "2026-03-26T12:00:00Z",
        "unsubscribed_at": None,
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.get(f"/notifications/status?wallet={VALID_WALLET}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_email"] is True
    assert body["verified"] is True
    assert body["unsubscribed"] is False


def test_status_no_email():
    mock_db = _mock_client_with_data([])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.get(f"/notifications/status?wallet={VALID_WALLET}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_email"] is False


# --- GET /notifications/unsubscribe ---


def test_unsubscribe_with_valid_token():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.verify_unsubscribe_token", return_value=True),
    ):
        resp = client.get(
            f"/notifications/unsubscribe?wallet={VALID_WALLET}&token=valid"
        )
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()


def test_unsubscribe_with_invalid_token():
    with patch("src.api.notifications.verify_unsubscribe_token", return_value=False):
        resp = client.get(f"/notifications/unsubscribe?wallet={VALID_WALLET}&token=bad")
    assert resp.status_code == 403


def test_unsubscribe_post_one_click():
    """RFC 8058 one-click POST unsubscribe should succeed with valid token."""
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.verify_unsubscribe_token", return_value=True),
    ):
        resp = client.post(
            f"/notifications/unsubscribe?wallet={VALID_WALLET}&token=valid"
        )
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()


def test_status_invalid_wallet():
    """GET /notifications/status with a malformed wallet returns 400."""
    resp = client.get("/notifications/status?wallet=not-an-address")
    assert resp.status_code == 400


def test_unsubscribe_db_failure_returns_502():
    """If the DB write fails, unsubscribe returns 502 instead of a false success."""
    mock_db = MagicMock()
    mock_db.table.return_value.update.return_value.eq.return_value.execute.side_effect = Exception(
        "db down"
    )
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.verify_unsubscribe_token", return_value=True),
    ):
        resp = client.get(
            f"/notifications/unsubscribe?wallet={VALID_WALLET}&token=valid"
        )
    assert resp.status_code == 502
