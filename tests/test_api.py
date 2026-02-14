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


def test_batch_status():
    response = client.get("/batch/status")
    assert response.status_code == 200
    data = response.json()
    assert "pending_orders" in data
    assert isinstance(data["pending_orders"], int)
    assert "batch_interval_minutes" in data
    assert "circuit_breaker" in data


def test_accept_empty_body():
    response = client.post("/accept", json={})
    assert response.status_code == 422


def test_accept_invalid_address():
    response = client.post("/accept", json={
        "user_address": "not_an_address",
        "option_type": "call",
        "strike": 2100,
        "expiry_days": 7,
        "premium": 50.0,
        "spot_at_lock": 2086.0,
        "iv_at_lock": 0.40,
    })
    assert response.status_code == 422


def test_accept_invalid_option_type():
    response = client.post("/accept", json={
        "user_address": VALID_ADDRESS,
        "option_type": "banana",
        "strike": 2100,
        "expiry_days": 7,
        "premium": 50.0,
        "spot_at_lock": 2086.0,
        "iv_at_lock": 0.40,
    })
    assert response.status_code == 422


def test_accept_negative_strike():
    response = client.post("/accept", json={
        "user_address": VALID_ADDRESS,
        "option_type": "call",
        "strike": -100,
        "expiry_days": 7,
        "premium": 50.0,
        "spot_at_lock": 2086.0,
        "iv_at_lock": 0.40,
    })
    assert response.status_code == 422
