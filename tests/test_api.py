from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

VALID_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_prices():
    """Integration test — calls Chainlink + Deribit live."""
    response = client.get("/prices")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 30  # 5 strikes × 3 expiries × 2 types
    first = data[0]
    assert "option_type" in first
    assert "strike" in first
    assert "premium" in first
    assert "delta" in first
    assert "available_amount" in first
    assert "ttl" in first
    assert first["premium"] > 0
    assert first["available_amount"] > 0


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


def test_accept_removed():
    """POST /accept no longer exists — orders are on-chain."""
    response = client.post("/accept", json={})
    assert response.status_code in (404, 405)


def test_batch_status_removed():
    """GET /batch/status no longer exists."""
    response = client.get("/batch/status")
    assert response.status_code == 404
