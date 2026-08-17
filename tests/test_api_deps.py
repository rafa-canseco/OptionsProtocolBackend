from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import HTTPException
import pytest

from src.api import deps


@pytest.fixture(autouse=True)
def reset_api_key_cache(monkeypatch):
    monkeypatch.setattr(deps, "_API_KEY_CACHE", {})
    monkeypatch.setattr(deps, "_API_KEY_CACHE_AT", 0.0)
    monkeypatch.setattr(deps, "_API_KEY_RETRY_AT", 0.0)


def test_auth_db_failure_opens_one_circuit_for_concurrent_requests(monkeypatch) -> None:
    worker_count = 8
    start = threading.Barrier(worker_count)
    calls = 0
    calls_lock = threading.Lock()

    def unavailable_client():
        nonlocal calls
        with calls_lock:
            calls += 1
        raise RuntimeError("database unavailable")

    def authenticate(index):
        start.wait(timeout=1)
        try:
            deps.require_mm_api_key(f"key-{index}")
        except HTTPException as exc:
            return exc.status_code
        return 200

    monkeypatch.setattr(deps, "get_client", unavailable_client)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        statuses = list(executor.map(authenticate, range(worker_count)))

    assert statuses == [503] * worker_count
    assert calls == 1


def test_auth_circuit_drops_stale_credentials_and_retries_after_window(
    monkeypatch,
) -> None:
    now = [100.0]
    calls = 0

    def unavailable_client():
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(deps.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(deps, "get_client", unavailable_client)
    monkeypatch.setattr(deps, "_API_KEY_CACHE", {"revoked-maybe": "0xabc"})
    monkeypatch.setattr(deps, "_API_KEY_CACHE_AT", 0.0)

    with pytest.raises(HTTPException) as first:
        deps.require_mm_api_key("revoked-maybe")
    with pytest.raises(HTTPException) as during_circuit:
        deps.require_mm_api_key("revoked-maybe")

    assert first.value.status_code == 503
    assert during_circuit.value.status_code == 503
    assert calls == 1
    assert deps._API_KEY_CACHE == {}

    now[0] += deps._API_KEY_RETRY_SECONDS
    with pytest.raises(HTTPException) as retried:
        deps.require_mm_api_key("revoked-maybe")

    assert retried.value.status_code == 503
    assert calls == 2


def test_auth_slow_failure_gets_full_cooldown_after_failure_observed(
    monkeypatch,
) -> None:
    now = [100.0]
    calls = 0

    def slow_unavailable_client():
        nonlocal calls
        calls += 1
        now[0] += 20.0
        raise RuntimeError("slow database failure")

    monkeypatch.setattr(deps.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(deps, "get_client", slow_unavailable_client)
    monkeypatch.setattr(deps, "_API_KEY_CACHE_AT", 100.0)

    with pytest.raises(HTTPException) as first:
        deps.require_mm_api_key("new-key")

    assert first.value.status_code == 503
    assert deps._API_KEY_RETRY_AT == 120.0 + deps._API_KEY_RETRY_SECONDS
    assert calls == 1

    now[0] = deps._API_KEY_RETRY_AT - 0.001
    with pytest.raises(HTTPException) as during_full_cooldown:
        deps.require_mm_api_key("new-key")

    assert during_full_cooldown.value.status_code == 503
    assert calls == 1

    now[0] = deps._API_KEY_RETRY_AT
    with pytest.raises(HTTPException) as retried:
        deps.require_mm_api_key("new-key")

    assert retried.value.status_code == 503
    assert calls == 2
