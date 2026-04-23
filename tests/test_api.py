import pytest
from fastapi.testclient import TestClient

import src.api.routes as routes_module
from src.main import app

client = TestClient(app)

VALID_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    """Clear in-memory rate-limit dicts between tests to prevent state leakage."""
    routes_module._waitlist_hits.clear()
    routes_module._read_hits.clear()
    yield
    routes_module._waitlist_hits.clear()
    routes_module._read_hits.clear()


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_prices():
    """Smoke test — prices come from mm_quotes DB table (may be empty)."""
    response = client.get("/prices")
    # 200 (quotes exist) or 503 (circuit breaker)
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


# --- CORS startup guard ---


def test_cors_wildcard_raises_in_production(monkeypatch):
    """CORS '*' with beta_mode=False must raise RuntimeError at startup."""
    import src.main as main_module

    monkeypatch.setattr(main_module.settings, "allowed_origins", "*")
    monkeypatch.setattr(main_module.settings, "beta_mode", False)
    with pytest.raises(RuntimeError, match="CORS cannot be"):
        with TestClient(main_module.app):
            pass  # lifespan fires on first request context enter


def test_cors_wildcard_allowed_in_beta(monkeypatch):
    """CORS '*' with beta_mode=True should start cleanly (only a warning)."""
    import src.main as main_module

    monkeypatch.setattr(main_module.settings, "allowed_origins", "*")
    monkeypatch.setattr(main_module.settings, "beta_mode", True)
    with TestClient(main_module.app) as c:
        response = c.get("/health")
    assert response.status_code == 200


# --- Rate limiting ---


def test_positions_rate_limit():
    """31st request from the same IP within 60s should return 429."""
    headers = {"X-Forwarded-For": "1.2.3.4"}
    for _ in range(30):
        r = client.get(f"/positions/{VALID_ADDRESS}", headers=headers)
        assert r.status_code == 200
    r = client.get(f"/positions/{VALID_ADDRESS}", headers=headers)
    assert r.status_code == 429


def test_positions_rate_limit_independent_ips():
    """Different IPs should have independent rate-limit buckets."""
    for i in range(30):
        r = client.get(
            f"/positions/{VALID_ADDRESS}",
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
        assert r.status_code == 200


def test_waitlist_count():
    """GET /waitlist/count should return a non-negative integer."""
    response = client.get("/waitlist/count")
    assert response.status_code == 200
    data = response.json()
    assert "count" in data
    assert isinstance(data["count"], int)
    assert data["count"] >= 0


def test_waitlist_count_rate_limit():
    """31st request from the same IP within 60s should return 429."""
    headers = {"X-Forwarded-For": "2.3.4.5"}
    for _ in range(30):
        r = client.get("/waitlist/count", headers=headers)
        assert r.status_code == 200
    r = client.get("/waitlist/count", headers=headers)
    assert r.status_code == 429
