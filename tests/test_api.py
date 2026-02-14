import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_prices():
    """Integration test — calls Chainlink + Deribit live."""
    response = client.get("/api/prices")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 30  # 5 strikes × 3 expiries × 2 types
    first = data[0]
    assert "option_type" in first
    assert "strike" in first
    assert "premium" in first
    assert "delta" in first
    assert first["premium"] > 0


def test_get_positions_empty():
    response = client.get("/api/positions/0xnonexistent")
    assert response.status_code == 200
    assert response.json() == []


def test_batch_status():
    response = client.get("/api/batch/status")
    assert response.status_code == 200
    data = response.json()
    assert "pending_orders" in data
    assert "batch_interval_minutes" in data
    assert "circuit_breaker" in data
