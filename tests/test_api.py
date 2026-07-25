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
    routes_module._prices_cache.clear()
    routes_module._prices_cached_at.clear()
    yield
    routes_module._waitlist_hits.clear()
    routes_module._read_hits.clear()
    routes_module._prices_cache.clear()
    routes_module._prices_cached_at.clear()


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


def test_get_tslax_spot_uses_solana_oracle():
    with pytest.MonkeyPatch.context() as mp:
        from src.chains.solana import oracle

        mp.setattr(oracle, "get_spot_price", lambda asset: (180.25, 1_700_000_000))
        response = client.get("/spot?asset=tslax")

    assert response.status_code == 200
    assert response.json() == {
        "asset": "tslax",
        "spot": 180.25,
        "updated_at": 1_700_000_000,
    }


def test_get_prices_strips_execution_fields_for_read_only_asset(monkeypatch):
    now = 1_900_000_000

    from src.chains.solana import oracle

    monkeypatch.setattr(routes_module.settings, "visible_assets", "eth,btc,sol,tslax")
    monkeypatch.setattr(routes_module.settings, "tradable_assets", "eth,btc")
    monkeypatch.setattr(oracle, "get_spot_price", lambda asset: (180.25, now))
    monkeypatch.setattr(
        routes_module,
        "_fetch_active_quotes",
        lambda asset: [
            {
                "bid_price": str(12_500_000),
                "max_amount": str(2 * 10**8),
                "deadline": now + 30,
                "strike_price": 180.0,
                "expiry": now + 7 * 86400,
                "is_put": True,
                "chain": "solana",
                "otoken_address": "H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF",
                "signature": "sig123",
                "mm_address": "maker123",
                "quote_id": "quote-1",
                "maker_nonce": 7,
            }
        ],
    )
    monkeypatch.setattr(
        routes_module,
        "_fetch_valid_otoken_addresses",
        lambda asset: {"H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF"},
    )
    monkeypatch.setattr(routes_module, "_fetch_position_counts", lambda asset: {})
    monkeypatch.setattr(
        routes_module.circuit_breaker, "is_paused_for", lambda asset: False
    )
    monkeypatch.setattr(
        routes_module.circuit_breaker, "check", lambda spot, asset: False
    )
    monkeypatch.setattr(
        routes_module.circuit_breaker,
        "update_reference",
        lambda spot, asset: None,
    )

    response = client.get("/prices?asset=tslax")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["premium"] == 12.0
    assert data[0]["spot"] == 180.25
    assert data[0]["chain"] == "solana"
    assert data[0]["otoken_address"] is None
    assert data[0]["signature"] is None
    assert data[0]["mm_address"] is None
    assert data[0]["bid_price_raw"] is None
    assert data[0]["deadline"] is None
    assert data[0]["quote_id"] is None
    assert data[0]["max_amount_raw"] is None
    assert data[0]["maker_nonce"] is None


def test_production_defaults_make_tslax_read_only(monkeypatch):
    now = 1_900_000_000

    from src.chains.solana import oracle

    monkeypatch.setattr(routes_module.settings, "app_env", "production")
    monkeypatch.setattr(routes_module.settings, "tradable_assets", None)
    monkeypatch.setattr(routes_module.settings, "visible_assets", "eth,btc,sol,tslax")
    monkeypatch.setattr(oracle, "get_spot_price", lambda asset: (180.25, now))
    monkeypatch.setattr(
        routes_module,
        "_fetch_active_quotes",
        lambda asset: [
            {
                "bid_price": str(12_500_000),
                "max_amount": str(2 * 10**8),
                "deadline": now + 30,
                "strike_price": 180.0,
                "expiry": now + 7 * 86400,
                "is_put": True,
                "chain": "solana",
                "otoken_address": "H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF",
                "signature": "sig123",
                "mm_address": "maker123",
                "quote_id": "quote-1",
                "maker_nonce": 7,
            }
        ],
    )
    monkeypatch.setattr(
        routes_module,
        "_fetch_valid_otoken_addresses",
        lambda asset: {"H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF"},
    )
    monkeypatch.setattr(routes_module, "_fetch_position_counts", lambda asset: {})
    monkeypatch.setattr(
        routes_module.circuit_breaker, "is_paused_for", lambda asset: False
    )
    monkeypatch.setattr(
        routes_module.circuit_breaker, "check", lambda spot, asset: False
    )
    monkeypatch.setattr(
        routes_module.circuit_breaker,
        "update_reference",
        lambda spot, asset: None,
    )

    response = client.get("/prices?asset=tslax")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["otoken_address"] is None
    assert data[0]["signature"] is None
    assert data[0]["quote_id"] is None


def test_invisible_asset_returns_404_on_spot(monkeypatch):
    monkeypatch.setattr(routes_module.settings, "visible_assets", "eth,btc")
    response = client.get("/spot?asset=tslax")
    assert response.status_code == 404


def test_invisible_asset_returns_404_on_capacity(monkeypatch):
    monkeypatch.setattr(routes_module.settings, "visible_assets", "eth,btc")
    response = client.get("/capacity?asset=tslax")
    assert response.status_code == 404


def test_invisible_asset_returns_404_on_prices(monkeypatch):
    monkeypatch.setattr(routes_module.settings, "visible_assets", "eth,btc")
    response = client.get("/prices?asset=tslax")
    assert response.status_code == 404


def test_tradable_asset_returns_execution_fields(monkeypatch):
    now = 1_900_000_000

    from src.chains.solana import oracle

    monkeypatch.setattr(routes_module.settings, "visible_assets", "eth,btc,sol,tslax")
    monkeypatch.setattr(routes_module.settings, "tradable_assets", "eth,btc,sol,tslax")
    monkeypatch.setattr(oracle, "get_spot_price", lambda asset: (180.25, now))
    monkeypatch.setattr(
        routes_module,
        "_fetch_active_quotes",
        lambda asset: [
            {
                "bid_price": str(12_500_000),
                "max_amount": str(2 * 10**8),
                "deadline": now + 30,
                "strike_price": 180.0,
                "expiry": now + 7 * 86400,
                "is_put": True,
                "chain": "solana",
                "otoken_address": "H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF",
                "signature": "sig123",
                "mm_address": "maker123",
                "quote_id": "quote-1",
                "maker_nonce": 7,
            }
        ],
    )
    monkeypatch.setattr(
        routes_module,
        "_fetch_valid_otoken_addresses",
        lambda asset: {"H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF"},
    )
    monkeypatch.setattr(routes_module, "_fetch_position_counts", lambda asset: {})
    monkeypatch.setattr(
        routes_module.circuit_breaker, "is_paused_for", lambda asset: False
    )
    monkeypatch.setattr(
        routes_module.circuit_breaker, "check", lambda spot, asset: False
    )
    monkeypatch.setattr(
        routes_module.circuit_breaker,
        "update_reference",
        lambda spot, asset: None,
    )

    response = client.get("/prices?asset=tslax")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["otoken_address"] == "H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF"
    assert data[0]["signature"] == "sig123"
    assert data[0]["mm_address"] == "maker123"
    assert data[0]["quote_id"] == "quote-1"
    assert data[0]["maker_nonce"] == 7


def test_get_spot_invalid_asset_clean_error():
    response = client.get("/spot?asset=notreal")
    assert response.status_code == 422


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


def test_fund_indexer_rpc_validation_precedes_task_creation(monkeypatch):
    import src.main as main_module

    monkeypatch.setattr(main_module.settings, "allowed_origins", "https://example.com")
    monkeypatch.setattr(main_module.settings, "tokenized_fund_indexer_enabled", True)
    monkeypatch.setattr(main_module.settings, "rpc_url", "")
    monkeypatch.setattr(
        main_module.asyncio,
        "create_task",
        lambda *_: pytest.fail("startup validation must precede task creation"),
    )

    with pytest.raises(RuntimeError, match="RPC_URL is required"):
        with TestClient(main_module.app):
            pass


@pytest.mark.parametrize(
    ("rpc_url", "private_keys", "message"),
    [
        ("", "0x" + f"{1:064x}", "RPC_URL"),
        ("https://rpc.example", "", "PRIVATE_KEYS"),
        ("https://rpc.example", "malformed", "Invalid.*PRIVATE_KEYS"),
        ("https://rpc.example", ", ,", "contains no keys"),
        (
            "https://rpc.example",
            ",".join(["0x" + f"{1:064x}"] * 2),
            "duplicate reporters",
        ),
    ],
)
def test_nav_reporter_validation_precedes_task_creation(
    monkeypatch, rpc_url, private_keys, message
):
    import src.main as main_module

    monkeypatch.setattr(main_module.settings, "allowed_origins", "https://example.com")
    monkeypatch.setattr(main_module.settings, "tokenized_fund_indexer_enabled", False)
    monkeypatch.setattr(main_module.settings, "fund_nav_reporter_enabled", True)
    monkeypatch.setattr(main_module.settings, "rpc_url", rpc_url)
    monkeypatch.setattr(
        main_module.settings, "fund_nav_reporter_private_keys", private_keys
    )
    monkeypatch.setattr(
        main_module.asyncio,
        "create_task",
        lambda *_: pytest.fail("startup validation must precede task creation"),
    )

    with pytest.raises(RuntimeError, match=message):
        with TestClient(main_module.app):
            pass


def test_nav_reporter_lease_is_bounded_before_task_creation(monkeypatch):
    import src.main as main_module

    monkeypatch.setattr(main_module.settings, "allowed_origins", "https://example.com")
    monkeypatch.setattr(main_module.settings, "tokenized_fund_indexer_enabled", False)
    monkeypatch.setattr(main_module.settings, "fund_nav_reporter_enabled", True)
    monkeypatch.setattr(main_module.settings, "rpc_url", "https://rpc.example")
    monkeypatch.setattr(
        main_module.settings,
        "fund_nav_reporter_private_keys",
        "0x" + f"{1:064x}",
    )
    monkeypatch.setattr(main_module.settings, "fund_nav_reporter_lease_seconds", 14)
    monkeypatch.setattr(
        main_module.asyncio,
        "create_task",
        lambda *_: pytest.fail("startup validation must precede task creation"),
    )

    with pytest.raises(RuntimeError, match="LEASE_SECONDS must be between 15 and 900"):
        with TestClient(main_module.app):
            pass


@pytest.mark.parametrize(
    ("reporter_enabled", "chain_id", "observer_keys", "message"),
    [
        (
            False,
            84532,
            ",".join(["0x" + f"{2:064x}", "0x" + f"{3:064x}"]),
            "FUND_NAV_REPORTER_ENABLED",
        ),
        (
            True,
            8453,
            ",".join(["0x" + f"{2:064x}", "0x" + f"{3:064x}"]),
            "restricted to Base Sepolia",
        ),
        (
            True,
            84532,
            ",".join(["0x" + f"{1:064x}", "0x" + f"{2:064x}"]),
            "separate from NAV reporter keys",
        ),
    ],
)
def test_sepolia_conservative_observation_validation_precedes_tasks(
    monkeypatch, reporter_enabled, chain_id, observer_keys, message
):
    import src.main as main_module

    monkeypatch.setattr(main_module.settings, "allowed_origins", "https://example.com")
    monkeypatch.setattr(main_module.settings, "tokenized_fund_indexer_enabled", False)
    monkeypatch.setattr(
        main_module.settings, "fund_nav_reporter_enabled", reporter_enabled
    )
    monkeypatch.setattr(main_module.settings, "rpc_url", "https://rpc.example")
    monkeypatch.setattr(
        main_module.settings,
        "fund_nav_reporter_private_keys",
        "0x" + f"{1:064x}",
    )
    monkeypatch.setattr(
        main_module.settings,
        "fund_csp_sepolia_conservative_observations_enabled",
        True,
    )
    monkeypatch.setattr(main_module.settings, "chain_id", chain_id)
    monkeypatch.setattr(
        main_module.settings,
        "fund_csp_sepolia_observer_private_keys",
        observer_keys,
    )
    monkeypatch.setattr(
        main_module.asyncio,
        "create_task",
        lambda *_: pytest.fail("startup validation must precede task creation"),
    )

    with pytest.raises(RuntimeError, match=message):
        with TestClient(main_module.app):
            pass


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
