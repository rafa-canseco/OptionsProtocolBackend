from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

VALID_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_prices():
    """Smoke test — prices come from mm_quotes DB table (may be empty)."""
    response = client.get("/prices")
    # 200 (quotes exist) or 200 with empty list (no quotes) or 503 (circuit breaker)
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        data = response.json()
        assert isinstance(data, list)


def test_get_positions_valid_address():
    response = client.get(f"/positions/{VALID_ADDRESS}")
    assert response.status_code == 200
    assert response.json() == []


def test_get_positions_invalid_address():
    response = client.get("/positions/0xnonexistent")
    assert response.status_code == 400


def test_get_positions_no_0x_prefix():
    response = client.get("/positions/1234567890abcdef1234567890abcdef12345678")
    assert response.status_code == 400


def test_waitlist_valid_email():
    response = client.post("/waitlist", json={"email": "test@example.com"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "new" in data


def test_waitlist_duplicate_email():
    """Duplicate email should still return 200."""
    client.post("/waitlist", json={"email": "dupe@example.com"})
    response = client.post("/waitlist", json={"email": "dupe@example.com"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["new"] is False


def test_waitlist_invalid_email():
    response = client.post("/waitlist", json={"email": "not-an-email"})
    assert response.status_code == 422


def test_waitlist_case_insensitive():
    """Mixed-case duplicate should be treated as same email."""
    client.post("/waitlist", json={"email": "CaseTest@Example.COM"})
    response = client.post("/waitlist", json={"email": "casetest@example.com"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["new"] is False


def test_waitlist_missing_email():
    response = client.post("/waitlist", json={})
    assert response.status_code == 422


def test_accept_removed():
    """POST /accept no longer exists — orders are on-chain."""
    response = client.post("/accept", json={})
    assert response.status_code in (404, 405)


def test_batch_status_removed():
    """GET /batch/status no longer exists."""
    response = client.get("/batch/status")
    assert response.status_code == 404
