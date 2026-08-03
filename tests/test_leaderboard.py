"""Unit coverage for the bounded opt-in legacy leaderboard API."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.leaderboard as leaderboard
from tests.leaderboard_oracle import (
    SEMANTIC_COVERAGE_MATRIX,
    snapshot,
    wallet_me,
    wallet_stats,
)


START = 1774828800
END = 1776038399
AS_OF = "2026-08-03T18:00:00+00:00"
ADDRESS = "0xaaaa000000000000000000000000000000000001"


class _Execution:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


class _RpcClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        value = self.responses[name]
        if isinstance(value, Exception):
            raise value
        return _Execution(value)

    def table(self, _name):
        raise AssertionError("leaderboard must never transfer raw order_events rows")


def _leaderboard_payload(*, limit=50, track1=None, track2=None, participants=1):
    return {
        "track1": track1 if track1 is not None else [],
        "track2": track2 if track2 is not None else [],
        "meta": {
            "competition_start": START,
            "competition_end": END,
            "total_participants": participants,
            "qualified_participants": participants,
            "_internal_raw_total_volume_usd": 500.0 * participants,
            "current_week": 2,
            "limit": limit,
            "truncated": participants > limit,
            "as_of": AS_OF,
            "cache_ttl_seconds": 60,
        },
    }


def _me_payload(address=ADDRESS, *, positions=0):
    return {
        "wallet": address,
        "position_count": positions,
        "_internal_raw_collateral_usd": 0.0,
        "total_earned_usd": 0.0,
        "earning_rate": None,
        "active_days": 0,
        "wheel_count": 0,
        "otm_streak": 0,
        "qualifies": False,
        "as_of": AS_OF,
        "cache_ttl_seconds": 60,
    }


@pytest.fixture(autouse=True)
def clear_cache():
    leaderboard._cache.clear()
    yield
    leaderboard._cache.clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(leaderboard.router)
    return TestClient(app)


def test_leaderboard_uses_one_bounded_rpc_and_exposes_freshness(monkeypatch, client):
    entry = {
        "rank": 1,
        "wallet": ADDRESS,
        "earning_rate": 0.001,
        "total_earned_usd": 0.5,
        "_internal_raw_collateral_usd": 500.0,
        "position_count": 2,
        "wheel_count": 1,
        "active_days": 2,
        "qualified": True,
        "progress": {"collateral_pct": 1.0},
    }
    rpc = _RpcClient(
        {"b1nary_legacy_leaderboard": _leaderboard_payload(track1=[entry])}
    )
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)

    response = client.get("/leaderboard")

    assert response.status_code == 200
    assert rpc.calls == [
        (
            "b1nary_legacy_leaderboard",
            {"p_start": START, "p_end": END, "p_limit": 50},
        )
    ]
    expected_entry = {**entry}
    expected_entry.pop("_internal_raw_collateral_usd")
    expected_entry["total_collateral_usd"] = 500.0
    assert response.json()["track1"] == [expected_entry]
    assert response.json()["meta"]["total_volume_usd"] == 500.0
    assert "_internal_raw_total_volume_usd" not in response.json()["meta"]
    assert response.json()["meta"]["cache_hit"] is False
    assert response.json()["meta"]["cache_age_seconds"] == 0.0
    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.headers["x-data-as-of"] == AS_OF


def test_cache_covers_empty_results_and_keys_every_filter(monkeypatch, client):
    clock = iter([100.0, 110.0, 110.0, 111.0])
    monkeypatch.setattr(leaderboard, "_monotonic", lambda: next(clock))
    rpc = _RpcClient(
        {"b1nary_legacy_leaderboard": _leaderboard_payload(limit=2, participants=0)}
    )
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)

    first = client.get("/leaderboard?limit=2")
    second = client.get("/leaderboard?limit=2")
    changed = client.get("/leaderboard?limit=2&start=1774828801")

    assert first.status_code == second.status_code == changed.status_code == 200
    assert len(rpc.calls) == 2
    assert first.json()["meta"]["cache_hit"] is False
    assert second.json()["meta"]["cache_hit"] is True
    assert second.json()["meta"]["cache_age_seconds"] == 10.0
    assert second.headers["age"] == "10"
    assert changed.json()["meta"]["cache_hit"] is False


def test_expired_entry_is_refetched(monkeypatch, client):
    clock = iter([1.0, 61.0, 61.0])
    monkeypatch.setattr(leaderboard, "_monotonic", lambda: next(clock))
    rpc = _RpcClient({"b1nary_legacy_leaderboard": _leaderboard_payload()})
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)

    assert client.get("/leaderboard").status_code == 200
    assert client.get("/leaderboard").status_code == 200
    assert len(rpc.calls) == 2


def test_leaderboard_rejects_unbounded_or_malformed_rpc_payload(monkeypatch, client):
    rpc = _RpcClient(
        {
            "b1nary_legacy_leaderboard": _leaderboard_payload(
                limit=1, track1=[{}, {}], participants=2
            )
        }
    )
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)
    assert client.get("/leaderboard?limit=1").status_code == 502

    leaderboard._cache.clear()
    rpc.responses["b1nary_legacy_leaderboard"] = {"track1": [], "track2": []}
    assert client.get("/leaderboard").status_code == 502


def test_leaderboard_db_error_returns_502(monkeypatch, client):
    rpc = _RpcClient({"b1nary_legacy_leaderboard": RuntimeError("offline")})
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)
    assert client.get("/leaderboard").status_code == 502


@pytest.mark.parametrize(
    "query",
    [
        f"start={END}&end={START}",
        f"start={START}&end={START + 91 * 24 * 3600}",
    ],
)
def test_leaderboard_rejects_invalid_ranges(client, query):
    assert client.get(f"/leaderboard?{query}").status_code == 400


@pytest.mark.parametrize("limit", [0, 101])
def test_leaderboard_rejects_limits_outside_rpc_bound(client, limit):
    assert client.get(f"/leaderboard?limit={limit}").status_code == 422


def test_me_is_fixed_cardinality_normalized_and_cached(monkeypatch, client):
    upper = ADDRESS.upper().replace("0X", "0x")
    rpc = _RpcClient({"b1nary_legacy_leaderboard_me": _me_payload(address=ADDRESS)})
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)

    first = client.get(f"/leaderboard/me?address={upper}")
    second = client.get(f"/leaderboard/me?address={ADDRESS}")

    assert first.status_code == second.status_code == 200
    assert first.json()["wallet"] == ADDRESS
    assert first.json()["position_count"] == 0
    assert first.json()["total_collateral_usd"] == 0.0
    assert "_internal_raw_collateral_usd" not in first.json()
    assert first.json()["cache_hit"] is False
    assert second.json()["cache_hit"] is True
    assert rpc.calls == [
        (
            "b1nary_legacy_leaderboard_me",
            {"p_address": ADDRESS, "p_start": START, "p_end": END},
        )
    ]


def test_api_uses_python_half_cent_rounding_for_leaderboard_and_me(monkeypatch, client):
    entry = {
        "rank": None,
        "wallet": ADDRESS,
        "earning_rate": 0.0,
        "total_earned_usd": 0.0,
        "_internal_raw_collateral_usd": 2.675,
        "position_count": 1,
        "wheel_count": 0,
        "active_days": 1,
        "qualified": False,
        "progress": {"collateral_pct": 0.0054},
    }
    me = _me_payload(address=ADDRESS, positions=1)
    me["_internal_raw_collateral_usd"] = 2.675
    rpc = _RpcClient(
        {
            "b1nary_legacy_leaderboard": _leaderboard_payload(
                track1=[entry], participants=1
            ),
            "b1nary_legacy_leaderboard_me": me,
        }
    )
    rpc.responses["b1nary_legacy_leaderboard"]["meta"][
        "_internal_raw_total_volume_usd"
    ] = 2.675
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)

    board = client.get("/leaderboard")
    personal = client.get(f"/leaderboard/me?address={ADDRESS}")

    assert board.status_code == personal.status_code == 200
    assert board.json()["track1"][0]["total_collateral_usd"] == 2.67
    assert board.json()["meta"]["total_volume_usd"] == 2.67
    assert personal.json()["total_collateral_usd"] == 2.67
    assert "_internal_raw_collateral_usd" not in board.text
    assert "_internal_raw_total_volume_usd" not in board.text
    assert "_internal_raw_collateral_usd" not in personal.text


def test_me_rejects_invalid_address_or_mismatched_payload(monkeypatch, client):
    assert client.get("/leaderboard/me?address=0xnotvalid").status_code == 400
    assert client.get("/leaderboard/me").status_code == 422

    rpc = _RpcClient(
        {
            "b1nary_legacy_leaderboard_me": _me_payload(
                address="0xbbbb000000000000000000000000000000000002"
            )
        }
    )
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)
    assert client.get(f"/leaderboard/me?address={ADDRESS}").status_code == 502


def test_migration_pins_security_bounds_and_shared_index():
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase/migrations/202608030004_b1n431_leaderboard_aggregates.sql"
    ).read_text()
    lowered = migration.lower()

    assert "idx_order_events_indexed_user_id" in lowered
    assert "idx_b1n431_order_events_user_indexed_id" in lowered
    assert lowered.count("security invoker") == 4
    assert lowered.count("set search_path = public, pg_temp") == 3
    assert lowered.count("set search_path = pg_catalog, pg_temp") == 1
    assert "immutable\nstrict\nparallel safe" in lowered
    assert "float8send(abs(p_value))" in lowered
    assert "from public, anon, authenticated" in lowered
    assert "to service_role" in lowered
    assert "limit 10000" not in lowered
    assert "select *" not in lowered
    assert "p_limit is null" in lowered
    assert "where e.user_address = normalized_address" in lowered
    assert lowered.count("'_internal_raw_collateral_usd'") == 2
    assert "'_internal_raw_total_volume_usd'" in lowered
    assert "track1_rows.raw_collateral_usd::numeric" not in lowered


# ---------------------------------------------------------------------------
# Retired golden semantic oracle
# ---------------------------------------------------------------------------


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def _row(
    row_id: int,
    *,
    wallet: str = ADDRESS,
    collateral: float = 50.0,
    net_premium: int | None = 50_000,
    premium: int | None = None,
    is_put: bool | None = True,
    is_itm: bool | None = False,
    asset: str = "eth",
    indexed_at: str = "2026-03-30T00:00:00+00:00",
    expiry: int | None = None,
    settled_at: str | None = None,
):
    return {
        "id": row_id,
        "user_address": wallet,
        "collateral_usd": collateral,
        "net_premium": net_premium,
        "premium": premium,
        "is_put": is_put,
        "is_itm": is_itm,
        "asset": asset,
        "indexed_at": indexed_at,
        "expiry": expiry if expiry is not None else _ts(indexed_at),
        "settled_at": settled_at,
    }


def _oracle(rows, *, start=START, end=END, limit=100):
    return snapshot(rows, start=start, end=end, limit=limit)


def test_qualifying_filter_collateral():
    rows = [_row(i, collateral=49.0) for i in range(1, 11)]
    result = _oracle(rows)
    assert result["meta"]["qualified_participants"] == 0
    assert result["track1"][0]["rank"] is None
    assert result["track1"][0]["qualified"] is False


def test_qualifying_filter_active_days():
    row = _row(1, collateral=600.0)
    result = _oracle([row])
    assert result["track1"][0]["active_days"] == 1
    assert result["track1"][0]["qualified"] is True
    assert result["track1"][0]["rank"] == 1


def test_active_days_merge_inclusive_overlapping_utc_ranges():
    rows = [
        _row(
            1,
            indexed_at="2026-03-30T23:59:59+00:00",
            expiry=_ts("2026-04-02T00:00:00+00:00"),
        ),
        _row(
            2,
            indexed_at="2026-04-01T00:00:00+00:00",
            expiry=_ts("2026-04-03T23:59:59+00:00"),
        ),
    ]
    assert wallet_stats(rows)["active_days"] == 5


def test_wheel_detection_applies_1_5x():
    rows = [
        _row(
            1,
            collateral=250,
            net_premium=100_000,
            is_put=True,
            is_itm=True,
            settled_at="2026-04-01T10:00:00+00:00",
            indexed_at="2026-04-01T08:00:00+00:00",
        ),
        _row(
            2,
            collateral=250,
            net_premium=100_000,
            is_put=False,
            is_itm=True,
            settled_at="2026-04-05T10:00:00+00:00",
            indexed_at="2026-04-01T20:00:00+00:00",
        ),
    ]
    stats = wallet_stats(rows)
    assert stats["wheel_count"] == 1
    assert stats["adjusted_premium"] == 0.3


def test_perfect_week_bonus():
    stats = wallet_stats(
        [
            _row(
                1,
                collateral=500,
                net_premium=100_000,
                is_itm=False,
                settled_at="2026-04-03T12:00:00+00:00",
            )
        ]
    )
    assert stats["adjusted_premium"] == 0.15


def test_wheel_priority_over_perfect_week():
    rows = [
        _row(
            1,
            collateral=250,
            net_premium=100_000,
            is_put=True,
            is_itm=True,
            settled_at="2026-04-02T08:00:00+00:00",
            indexed_at="2026-04-02T07:00:00+00:00",
        ),
        _row(
            2,
            collateral=250,
            net_premium=100_000,
            is_put=False,
            is_itm=True,
            settled_at="2026-04-04T08:00:00+00:00",
            indexed_at="2026-04-02T16:00:00+00:00",
        ),
    ]
    stats = wallet_stats(rows)
    assert stats["wheel_count"] == 1
    assert stats["adjusted_premium"] == 0.3  # one 1.5x multiplier per leg


def test_otm_streak_basic():
    outcomes = [False, False, False, True, False, False]
    rows = [
        _row(
            index,
            is_itm=outcome,
            settled_at=f"2026-04-01T0{index}:00:00+00:00",
        )
        for index, outcome in enumerate(outcomes, start=1)
    ]
    assert wallet_stats(rows)["otm_streak"] == 3


def test_track1_ranking():
    wallet_a = "0xaaaa00000000000000000000000000000000000a"
    wallet_b = "0xbbbb00000000000000000000000000000000000b"
    rows = [
        _row(1, wallet=wallet_a, collateral=500, net_premium=200_000),
        _row(2, wallet=wallet_b, collateral=500, net_premium=100_000),
    ]
    track = _oracle(rows)["track1"]
    assert [(row["wallet"], row["rank"]) for row in track] == [
        (wallet_a, 1),
        (wallet_b, 2),
    ]


def test_metadata_fields():
    result = _oracle([_row(1, collateral=500)])
    assert result["meta"] == {
        "competition_start": START,
        "competition_end": END,
        "total_participants": 1,
        "qualified_participants": 1,
        "total_volume_usd": 500.0,
        "limit": 100,
        "truncated": False,
    }


def test_perfect_week_week2_bonus():
    stats = wallet_stats(
        [
            _row(
                1,
                collateral=500,
                net_premium=100_000,
                is_itm=False,
                settled_at="2026-04-09T12:00:00+00:00",
            )
        ]
    )
    assert stats["adjusted_premium"] == 0.15


def test_cross_asset_wheel_rejected():
    rows = [
        _row(
            1,
            is_put=True,
            is_itm=True,
            asset="eth",
            settled_at="2026-04-01T10:00:00+00:00",
        ),
        _row(
            2,
            is_put=False,
            is_itm=True,
            asset="btc",
            indexed_at="2026-04-01T12:00:00+00:00",
            settled_at="2026-04-02T10:00:00+00:00",
        ),
    ]
    assert wallet_stats(rows)["wheel_count"] == 0


def test_db_exception_returns_502(monkeypatch, client):
    rpc = _RpcClient({"b1nary_legacy_leaderboard": RuntimeError("DB down")})
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)
    assert client.get("/leaderboard").status_code == 502


def test_db_none_data_returns_502(monkeypatch, client):
    rpc = _RpcClient({"b1nary_legacy_leaderboard": None})
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)
    assert client.get("/leaderboard").status_code == 502


def test_boundary_collateral_exactly_500_qualifies():
    result = _oracle([_row(1, collateral=500)])
    assert result["meta"]["qualified_participants"] == 1
    assert result["track1"][0]["total_collateral_usd"] == 500.0
    assert result["track1"][0]["qualified"] is True


def test_raw_499_996_displays_500_without_qualifying():
    result = _oracle([_row(1, collateral=499.996)])
    assert result["meta"]["qualified_participants"] == 0
    assert result["track1"][0]["total_collateral_usd"] == 500.0
    assert result["track1"][0]["qualified"] is False
    assert result["track1"][0]["rank"] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(2.675, 2.67), (2.685, 2.69), (2.665, 2.67), (2.655, 2.65)],
)
def test_oracle_preserves_predecessor_python_half_cent_rounding(raw, expected):
    result = _oracle([_row(1, collateral=raw)])
    assert result["track1"][0]["total_collateral_usd"] == expected
    assert result["meta"]["total_volume_usd"] == expected
    assert (
        wallet_me([_row(1, collateral=raw)], address=ADDRESS, start=START, end=END)[
            "total_collateral_usd"
        ]
        == expected
    )


def test_oracle_preserves_progress_boundary_rounding():
    entry = _oracle([_row(1, collateral=0.075, net_premium=0)])["track1"][0]
    assert entry["progress"]["collateral_pct"] == 0.0001


def test_oracle_seven_unit_perfect_week_preserves_qualified_rank_tie():
    lower = "0x0000000000000000000000000000000000004001"
    seven_unit = "0x0000000000000000000000000000000000004003"
    result = _oracle(
        [
            _row(
                1,
                wallet=lower,
                collateral=501,
                net_premium=0,
                is_itm=True,
                settled_at="2026-04-04T12:00:01+00:00",
            ),
            _row(
                2,
                wallet=seven_unit,
                collateral=501,
                net_premium=7,
                is_itm=False,
                settled_at="2026-04-04T12:00:02+00:00",
            ),
        ]
    )
    assert [row["wallet"] for row in result["track1"]] == [lower, seven_unit]
    assert [row["earning_rate"] for row in result["track1"]] == [0.0, 0.0]
    assert [row["rank"] for row in result["track1"]] == [1, 2]
    assert result["track1"][1]["total_earned_usd"] == 0.000010


def test_oracle_rounded_earning_rate_preserves_qualified_rank_tie():
    lower = "0x0000000000000000000000000000000000004001"
    boundary = "0x0000000000000000000000000000000000004002"
    result = _oracle(
        [
            _row(
                1,
                wallet=lower,
                collateral=501,
                net_premium=0,
                is_itm=True,
                settled_at="2026-04-04T12:00:01+00:00",
            ),
            _row(
                2,
                wallet=boundary,
                collateral=501,
                net_premium=167,
                is_itm=False,
                settled_at="2026-04-04T12:00:02+00:00",
            ),
        ]
    )
    assert [row["wallet"] for row in result["track1"]] == [lower, boundary]
    assert [row["earning_rate"] for row in result["track1"]] == [0.0, 0.0]
    assert [row["rank"] for row in result["track1"]] == [1, 2]
    assert result["track1"][1]["total_earned_usd"] == 0.000250


def test_subcent_metadata_rounds_after_summing_raw_wallet_totals():
    rows = [
        _row(
            index,
            wallet=f"0x{index:040x}",
            collateral=0.004,
        )
        for index in range(1, 101)
    ]
    result = _oracle(rows)
    assert result["meta"]["total_volume_usd"] == 0.4
    assert sum(row["total_collateral_usd"] for row in result["track1"]) == 0


def test_track1_uses_raw_collateral_as_the_rate_tie_break():
    lower = "0xaaaa00000000000000000000000000000000000a"
    higher = "0xbbbb00000000000000000000000000000000000b"
    result = _oracle(
        [
            _row(1, wallet=lower, collateral=500.003),
            _row(2, wallet=higher, collateral=500.004),
        ]
    )
    assert [row["total_collateral_usd"] for row in result["track1"]] == [500.0, 500.0]
    assert [row["wallet"] for row in result["track1"]] == [higher, lower]


def test_single_position_qualifies_on_collateral_alone():
    result = _oracle([_row(1, collateral=600)])
    assert result["track1"][0]["rank"] == 1
    assert result["track1"][0]["active_days"] == 1


def test_start_gte_end_returns_400(client):
    assert client.get(f"/leaderboard?start={END}&end={START}").status_code == 400


def test_leaderboard_me_no_positions():
    result = wallet_me([], address=ADDRESS, start=START, end=END)
    assert result == {
        "wallet": ADDRESS,
        "position_count": 0,
        "total_collateral_usd": 0.0,
        "total_earned_usd": 0.0,
        "earning_rate": None,
        "active_days": 0,
        "wheel_count": 0,
        "otm_streak": 0,
        "qualifies": False,
    }


def test_leaderboard_me_below_threshold_returns_stats():
    rows = [_row(1, collateral=49), _row(2, collateral=49)]
    result = wallet_me(rows, address=ADDRESS, start=START, end=END)
    assert result["total_collateral_usd"] == 98.0
    assert result["qualifies"] is False
    assert result["earning_rate"] is not None


def test_leaderboard_me_qualifying_wallet():
    result = wallet_me([_row(1, collateral=500)], address=ADDRESS, start=START, end=END)
    assert result["qualifies"] is True
    assert result["position_count"] == 1
    assert set(result) == {
        "wallet",
        "position_count",
        "total_collateral_usd",
        "total_earned_usd",
        "earning_rate",
        "active_days",
        "wheel_count",
        "otm_streak",
        "qualifies",
    }


def test_leaderboard_me_invalid_address(client):
    assert client.get("/leaderboard/me?address=0xnotvalid").status_code == 400


def test_leaderboard_me_missing_address(client):
    assert client.get("/leaderboard/me").status_code == 422


def test_qualified_wallet_has_rank_and_flag():
    entry = _oracle([_row(1, collateral=500)])["track1"][0]
    assert entry["rank"] == 1
    assert entry["qualified"] is True
    assert entry["progress"]["collateral_pct"] == 1.0


def test_mixed_qualified_and_non_qualified_ordering():
    qualified = "0xaaaa00000000000000000000000000000000000a"
    unqualified = "0xbbbb00000000000000000000000000000000000b"
    rows = [
        _row(1, wallet=qualified, collateral=500, net_premium=1),
        _row(2, wallet=unqualified, collateral=499, net_premium=1_000_000),
    ]
    track = _oracle(rows)["track1"]
    assert [row["wallet"] for row in track] == [qualified, unqualified]
    assert [row["rank"] for row in track] == [1, None]


def test_progress_values_capped_at_1():
    entry = _oracle([_row(1, collateral=2_000)])["track1"][0]
    assert entry["progress"]["collateral_pct"] == 1.0


def test_wheel_requires_both_assignments():
    rows = [
        _row(
            1,
            is_put=True,
            is_itm=True,
            settled_at="2026-04-01T10:00:00+00:00",
        ),
        _row(
            2,
            is_put=False,
            is_itm=False,
            indexed_at="2026-04-01T20:00:00+00:00",
            settled_at="2026-04-05T10:00:00+00:00",
        ),
    ]
    assert wallet_stats(rows)["wheel_count"] == 0


def test_perfect_week_is_suppressed_by_itm_in_same_fixed_week():
    rows = [
        _row(
            1, net_premium=100_000, is_itm=False, settled_at="2026-04-01T01:00:00+00:00"
        ),
        _row(
            2, net_premium=100_000, is_itm=True, settled_at="2026-04-02T01:00:00+00:00"
        ),
    ]
    assert wallet_stats(rows)["adjusted_premium"] == 0.2


def test_track_ties_use_wallet_ascending_and_ranks_are_sequential():
    wallets = [
        "0xcccc00000000000000000000000000000000000c",
        "0xaaaa00000000000000000000000000000000000a",
        "0xbbbb00000000000000000000000000000000000b",
    ]
    rows = [
        _row(index, wallet=wallet, collateral=500)
        for index, wallet in enumerate(wallets, 1)
    ]
    result = _oracle(rows)
    expected = sorted(wallets)
    assert [row["wallet"] for row in result["track1"]] == expected
    assert [row["rank"] for row in result["track1"]] == [1, 2, 3]
    assert [row["wallet"] for row in result["track2"]] == expected


def test_date_boundaries_are_inclusive_and_arbitrary_ranges_filter_first():
    rows = [
        _row(1, indexed_at="2026-03-30T00:00:00+00:00"),
        _row(2, indexed_at="2026-04-12T23:59:59+00:00"),
        _row(3, indexed_at="2026-04-13T00:00:00+00:00"),
    ]
    assert _oracle(rows)["track1"][0]["position_count"] == 2
    arbitrary_start = _ts("2026-04-12T00:00:00+00:00")
    arbitrary_end = _ts("2026-04-13T00:00:00+00:00")
    assert (
        _oracle(rows, start=arbitrary_start, end=arbitrary_end)["track1"][0][
            "position_count"
        ]
        == 2
    )


def test_premium_fallbacks_preserve_zero_and_default_to_zero():
    rows = [
        _row(1, net_premium=None, premium=200_000),
        _row(2, net_premium=0, premium=900_000),
        _row(3, net_premium=None, premium=None),
    ]
    assert wallet_stats(rows)["adjusted_premium"] == 0.2


def test_semantic_matrix_has_oracle_and_real_postgres_coverage():
    assert len(SEMANTIC_COVERAGE_MATRIX) == 17
    assert all(
        "oracle-unit" in coverage for coverage in SEMANTIC_COVERAGE_MATRIX.values()
    )
    assert all(
        any(item.startswith("postgres-") for item in coverage)
        for coverage in SEMANTIC_COVERAGE_MATRIX.values()
    )


def test_me_cache_headers_are_private(monkeypatch, client):
    rpc = _RpcClient({"b1nary_legacy_leaderboard_me": _me_payload()})
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc)
    response = client.get(f"/leaderboard/me?address={ADDRESS}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=60"
