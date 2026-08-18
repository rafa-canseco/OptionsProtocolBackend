import concurrent.futures
from decimal import Decimal
import os
import time

import httpx
import pytest

from tests.weekly_aggregator_oracle import b1n434_scale_snapshot, business_digest


pytestmark = pytest.mark.integration

START = "2026-04-03T08:00:00+00:00"
TOKYO_START = "2026-04-03T17:00:00+09:00"
END = "2026-04-10T08:00:00+00:00"
EMPTY_START = "2026-04-10T08:00:00+00:00"
EMPTY_END = "2026-04-17T08:00:00+00:00"
SCALE_WALLET = "0x0000000000000000000000000000000000000002"
TIE_WALLET = "0x0000000000000000000000000000000000005001"
OHLC = {
    "p_eth_open": 2100.125,
    "p_eth_close": 2000.0,
    "p_eth_high": 2200.125,
    "p_eth_low": 1900.125,
}


def _required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing integration environment: {name}")
    return value


@pytest.fixture(scope="module")
def postgrest_url():
    return _required("B1N434_POSTGREST_URL")


@pytest.fixture(scope="module")
def tokens():
    return {
        role: _required(f"B1N434_{role.upper()}_TOKEN")
        for role in ("anon", "authenticated", "service")
    }


def _headers(token, *, count=False):
    headers = {"Authorization": f"Bearer {token}"}
    if count:
        headers["Prefer"] = "count=exact"
    return headers


def _rpc(url, token, name, payload, *, timeout=60, timezone_name=None):
    headers = _headers(token)
    if timezone_name:
        headers["Prefer"] = f"timezone={timezone_name}"
    return httpx.post(
        f"{url}/rpc/{name}",
        headers=headers,
        json=payload,
        timeout=timeout,
    )


def _preflight_payload(start=START, end=END):
    return {"p_week_start": start, "p_week_end": end}


def _write_payload(start=START, end=END, **overrides):
    payload = {"p_week_start": start, "p_week_end": end, **OHLC}
    payload.update(overrides)
    return payload


def _state(url, token):
    response = _rpc(url, token, "b1n434_fixture_state", {})
    response.raise_for_status()
    return response.json()


def _set_failure(url, token, enabled):
    response = _rpc(url, token, "b1n434_fixture_set_failure", {"p_enabled": enabled})
    response.raise_for_status()


def _set_hold(url, token, seconds):
    response = _rpc(
        url,
        token,
        "b1n434_fixture_set_hold",
        {"p_seconds": seconds},
    )
    response.raise_for_status()


def _business_digest(url, token):
    response = _rpc(url, token, "b1n434_fixture_business_digest", {})
    response.raise_for_status()
    return response.json()


def _try_week_lock(url, token, *, week_start, timezone_name):
    response = _rpc(
        url,
        token,
        "b1n434_fixture_try_week_lock",
        {"p_week_start": week_start},
        timezone_name=timezone_name,
    )
    response.raise_for_status()
    assert f"timezone={timezone_name}" in response.headers.get("preference-applied", "")
    return response.json()


def _binary64_sum(values):
    total = 0.0
    for value in values:
        total += float(value)
    return total


def _contains_index(value, name):
    if isinstance(value, dict):
        return value.get("Index Name") == name or any(
            _contains_index(item, name) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_index(item, name) for item in value)
    return False


def test_preflight_is_exact_fixed_cardinality_beyond_row_cap_and_service_only(
    postgrest_url, tokens
):
    service = tokens["service"]
    count = httpx.get(
        f"{postgrest_url}/order_events",
        headers=_headers(service, count=True),
        params={
            "select": "id",
            "indexed_at": "gte.2026-04-03T08:00:00+00:00",
            "and": "(indexed_at.lt.2026-04-10T08:00:00+00:00)",
            "limit": "1",
        },
        timeout=30,
    )
    count.raise_for_status()
    assert count.headers["content-range"].endswith("/100000")

    started = time.perf_counter()
    response = _rpc(
        postgrest_url,
        service,
        "b1nary_legacy_week_source",
        _preflight_payload(),
    )
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    assert response.json()["has_rows"] is True
    assert response.json()["source_rows"] == 100_000
    assert set(response.json()) == {
        "has_rows",
        "source_rows",
        "week_start",
        "week_end",
        "source_max_indexed_at",
        "source_max_id",
    }
    assert len(response.content) < 512
    assert elapsed < 15

    for role in ("anon", "authenticated"):
        denied = _rpc(
            postgrest_url,
            tokens[role],
            "b1nary_legacy_week_source",
            _preflight_payload(),
        )
        assert denied.status_code in (401, 403, 404)

    print(
        "B1N-434 preflight evidence: source_rows=100000 "
        f"response_bytes={len(response.content)} seconds={elapsed:.3f}"
    )


def test_atomic_rollback_on_induced_report_error(postgrest_url, tokens):
    service = tokens["service"]
    before = _state(postgrest_url, service)
    assert before["user_rows"] == 2
    assert before["report_rows"] == 0

    _set_failure(postgrest_url, service, True)
    failed = _rpc(
        postgrest_url,
        service,
        "b1nary_aggregate_legacy_week",
        _write_payload(),
        timeout=60,
    )
    assert failed.status_code >= 400
    assert "induced report failure" in failed.text
    assert _state(postgrest_url, service) == before
    _set_failure(postgrest_url, service, False)


def test_atomic_write_matches_100k_oracle_with_fixed_rows_bytes_time_and_grants(
    postgrest_url, tokens
):
    service = tokens["service"]
    for role in ("anon", "authenticated"):
        denied = _rpc(
            postgrest_url,
            tokens[role],
            "b1nary_aggregate_legacy_week",
            _write_payload(),
        )
        assert denied.status_code in (401, 403, 404)

    started = time.perf_counter()
    response = _rpc(
        postgrest_url,
        service,
        "b1nary_aggregate_legacy_week",
        _write_payload(),
        timeout=60,
    )
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    payload = response.json()
    assert payload["status"] == "aggregated"
    assert payload["source_rows"] == 100_000
    assert payload["wallet_rows"] == 2_001
    assert payload["assignments"] == 5_005
    assert set(payload) == {
        "status",
        "week_start",
        "week_end",
        "source_rows",
        "wallet_rows",
        "assignments",
        "source_max_indexed_at",
        "source_max_id",
    }
    assert len(response.content) < 512
    assert elapsed < 30

    result_count = httpx.get(
        f"{postgrest_url}/user_weekly_results",
        headers=_headers(service, count=True),
        params={"select": "id", "week_start": "eq.2026-04-03", "limit": "1"},
        timeout=30,
    )
    result_count.raise_for_status()
    assert result_count.headers["content-range"].endswith("/2001")

    selected = httpx.get(
        f"{postgrest_url}/user_weekly_results",
        headers=_headers(service),
        params={
            "select": (
                "user_address,positions_opened,total_simulated_premium,assignments,"
                "simulated_pnl,cumulative_pnl"
            ),
            "user_address": f"in.({SCALE_WALLET},{TIE_WALLET})",
            "week_start": "eq.2026-04-03",
            "order": "user_address.asc",
        },
        timeout=30,
    )
    selected.raise_for_status()
    expected_users, expected_report = b1n434_scale_snapshot()
    assert selected.json() == [
        expected_users[SCALE_WALLET],
        expected_users[TIE_WALLET],
    ]
    assert expected_users[TIE_WALLET]["total_simulated_premium"] == 1.0968

    # The fixture's wallet insertion order is the production report order:
    # first indexed_at, then wallet. It intentionally distinguishes the pinned
    # Python binary64 result from exact NUMERIC and reverse-order alternatives.
    wallet_totals = [
        item["total_simulated_premium"] for item in expected_users.values()
    ]
    exact_total = sum(Decimal(str(value)) for value in wallet_totals)
    reverse_binary64_total = round(_binary64_sum(reversed(wallet_totals)), 4)
    assert expected_report["total_simulated_premium"] == 100000010990.3477
    assert exact_total == Decimal("100000010990.3476")
    assert reverse_binary64_total == 100000010990.3476
    assert expected_report["total_simulated_premium"] != float(exact_total)
    assert expected_report["total_simulated_premium"] != reverse_binary64_total

    # One fixed-cardinality digest compares all six business fields for every
    # target wallet, including all 2,001 rows beyond PostgREST's row cap.
    actual_digest = _business_digest(postgrest_url, service)
    assert actual_digest == {
        "rows": 2_001,
        "sha256": business_digest(expected_users.values()),
    }

    report = httpx.get(
        f"{postgrest_url}/weekly_reports",
        headers=_headers(service),
        params={
            "select": (
                "total_users,total_positions,total_simulated_premium,"
                "total_assignments,eth_open,eth_close,eth_high,eth_low,narrative_data"
            ),
            "week_start": "eq.2026-04-03",
        },
        timeout=30,
    )
    report.raise_for_status()
    assert report.json() == [expected_report]
    print(
        "B1N-434 write evidence: source_rows=100000 wallet_rows=2001 "
        f"response_bytes={len(response.content)} seconds={elapsed:.3f}"
    )


def test_identical_retry_preserves_every_column_and_fixed_response(
    postgrest_url, tokens
):
    service = tokens["service"]
    before = _state(postgrest_url, service)
    time.sleep(1.1)

    retry = _rpc(
        postgrest_url,
        service,
        "b1nary_aggregate_legacy_week",
        _write_payload(),
        timeout=60,
    )
    retry.raise_for_status()
    assert retry.json()["wallet_rows"] == 2_001
    # The state digest covers IDs, every business field, and created/updated
    # timestamps for both tables. Waiting proves a hidden now() update would fail.
    assert _state(postgrest_url, service) == before


def test_direct_canonical_lock_probe_across_timezones_and_coherent_snapshot(
    postgrest_url, tokens
):
    service = tokens["service"]
    _set_hold(postgrest_url, service, 3.0)
    # Differ from the snapshot written by the earlier tests so the first real
    # transaction reaches the fixture's report guard and stays open long enough
    # for the advisory-only probe. The second transaction then overwrites it.
    first_payload = _write_payload(
        p_eth_open=2125.25,
        p_eth_close=2025.0,
        p_eth_high=2225.5,
        p_eth_low=1875.25,
    )
    second_ohlc = {
        "p_eth_open": 2150.25,
        "p_eth_close": 2050.0,
        "p_eth_high": 2250.5,
        "p_eth_low": 1850.25,
    }
    second_payload = _write_payload(**second_ohlc)

    def invoke(payload, timezone_name):
        response = _rpc(
            postgrest_url,
            service,
            "b1nary_aggregate_legacy_week",
            payload,
            timeout=60,
            timezone_name=timezone_name,
        )
        response.raise_for_status()
        assert f"timezone={timezone_name}" in response.headers.get(
            "preference-applied", ""
        )
        return response.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, first_payload, "America/Los_Angeles")

        # Probe only the exact production advisory key. A false result directly
        # proves the first real aggregation transaction holds it; this RPC never
        # reads or writes the fixture control/report rows and cannot be blocked by
        # their row or trigger locks. Polling only closes the request-start race.
        deadline = time.monotonic() + 10
        during = None
        while time.monotonic() < deadline and not first.done():
            candidate = _try_week_lock(
                postgrest_url,
                service,
                week_start=TOKYO_START,
                timezone_name="Asia/Tokyo",
            )
            if candidate["acquired"] is False:
                during = candidate
                break
            time.sleep(0.05)
        assert during is not None
        assert not first.done()
        assert during == {
            "acquired": False,
            "canonical_lock_input": "b1n434:2026-04-03",
            "canonical_lock_key": during["canonical_lock_key"],
            "canonical_week_start": "2026-04-03",
            "hash_seed": 434,
        }

        # A second real transaction in the probe's timezone must serialize after
        # the first. The direct false result above, not elapsed time or row-lock
        # behavior, is the lock proof.
        second = pool.submit(invoke, second_payload, "Asia/Tokyo")
        first_result = first.result()
        second_result = second.result()

    after = _try_week_lock(
        postgrest_url,
        service,
        week_start=START,
        timezone_name="America/Los_Angeles",
    )
    assert after["acquired"] is True
    assert after["canonical_lock_key"] == during["canonical_lock_key"]
    assert after["canonical_lock_input"] == during["canonical_lock_input"]
    assert first_result["wallet_rows"] == second_result["wallet_rows"] == 2_001

    expected_users, expected_report = b1n434_scale_snapshot(
        eth_open=second_ohlc["p_eth_open"],
        eth_close=second_ohlc["p_eth_close"],
        eth_high=second_ohlc["p_eth_high"],
        eth_low=second_ohlc["p_eth_low"],
    )
    assert _business_digest(postgrest_url, service) == {
        "rows": 2_001,
        "sha256": business_digest(expected_users.values()),
    }
    report = httpx.get(
        f"{postgrest_url}/weekly_reports",
        headers=_headers(service),
        params={
            "select": (
                "total_users,total_positions,total_simulated_premium,"
                "total_assignments,eth_open,eth_close,eth_high,eth_low,"
                "narrative_data"
            ),
            "week_start": "eq.2026-04-03",
        },
        timeout=30,
    )
    report.raise_for_status()
    assert report.json() == [expected_report]


def test_empty_week_has_one_preflight_and_no_write(postgrest_url, tokens):
    service = tokens["service"]
    before = _state(postgrest_url, service)
    response = _rpc(
        postgrest_url,
        service,
        "b1nary_legacy_week_source",
        _preflight_payload(EMPTY_START, EMPTY_END),
    )
    response.raise_for_status()
    assert response.json()["has_rows"] is False
    assert response.json()["source_rows"] == 0
    assert _state(postgrest_url, service) == before


def test_rejects_invalid_inputs_and_unsafe_backfill_without_changes(
    postgrest_url, tokens
):
    service = tokens["service"]
    before = _state(postgrest_url, service)
    invalid_payloads = [
        _write_payload(end="2026-04-10T07:59:59+00:00"),
        _write_payload(p_eth_high=1999.0),
        _write_payload(p_eth_low=2001.0),
        _write_payload(p_eth_open=-1.0),
    ]
    for payload in invalid_payloads:
        response = _rpc(
            postgrest_url,
            service,
            "b1nary_aggregate_legacy_week",
            payload,
        )
        assert response.status_code >= 400
        assert _state(postgrest_url, service) == before

    unsafe = _rpc(
        postgrest_url,
        service,
        "b1nary_aggregate_legacy_week",
        _write_payload(
            start="2026-03-27T08:00:00+00:00",
            end="2026-04-03T08:00:00+00:00",
        ),
    )
    assert unsafe.status_code >= 400
    assert "unsafe historical recomputation" in unsafe.text
    assert _state(postgrest_url, service) == before


def test_shared_generic_source_index_is_used_naturally(postgrest_url, tokens):
    response = _rpc(
        postgrest_url,
        tokens["service"],
        "b1n434_fixture_query_plan",
        {},
    )
    response.raise_for_status()
    assert _contains_index(response.json(), "idx_order_events_indexed_user_id")
