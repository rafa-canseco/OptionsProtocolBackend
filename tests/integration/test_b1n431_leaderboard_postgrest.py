import hashlib
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.leaderboard as leaderboard
from tests.leaderboard_oracle import snapshot, wallet_me


pytestmark = pytest.mark.integration

START = 1_774_828_800
END = 1_776_038_399
START_DT = datetime(2026, 3, 30, tzinfo=timezone.utc)
END_DT = datetime(2026, 4, 12, 23, 59, 59, tzinfo=timezone.utc)
SCALE_WALLET = "0x0000000000000000000000000000000000000002"
EMPTY_WALLET = "0x0000000000000000000000000000000000000041"
FALLBACK_WALLET = "0x0000000000000000000000000000000000000010"
SUBCENT_START = int(datetime(2026, 4, 11, 12, 0, 1, tzinfo=timezone.utc).timestamp())
SUBCENT_END = int(datetime(2026, 4, 11, 12, 3, 20, tzinfo=timezone.utc).timestamp())
ROUNDING_BASE = datetime(2026, 4, 11, 13, 0, tzinfo=timezone.utc)
ROUNDING_WALLETS = (
    ("0x0000000000000000000000000000000000003001", 2.675, 2.67),
    ("0x0000000000000000000000000000000000003002", 2.685, 2.69),
    ("0x0000000000000000000000000000000000003003", 2.665, 2.67),
    ("0x0000000000000000000000000000000000003004", 2.655, 2.65),
)
PREDECESSOR_ROUNDING_BASE = datetime(2026, 4, 11, 14, 0, tzinfo=timezone.utc)
PROGRESS_WALLET = "0x0000000000000000000000000000000000004004"
SEVEN_UNIT_RANK_WALLETS = (
    "0x0000000000000000000000000000000000004001",
    "0x0000000000000000000000000000000000004003",
)
RATE_TIE_WALLETS = (
    "0x0000000000000000000000000000000000005001",
    "0x0000000000000000000000000000000000005002",
)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing integration environment: {name}")
    return value


@pytest.fixture(scope="module")
def postgrest_url() -> str:
    return _required_env("B1N431_POSTGREST_URL")


@pytest.fixture(scope="module")
def service_token() -> str:
    return _required_env("B1N431_SERVICE_TOKEN")


def _headers(token: str, *, prefer: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _rpc(postgrest_url: str, token: str, name: str, payload: dict):
    return httpx.post(
        f"{postgrest_url}/rpc/{name}",
        headers=_headers(token),
        json=payload,
        timeout=60,
    )


class _PostgrestRpcExecution:
    def __init__(self, postgrest_url: str, token: str, name: str, payload: dict):
        self.postgrest_url = postgrest_url
        self.token = token
        self.name = name
        self.payload = payload

    def execute(self):
        response = _rpc(self.postgrest_url, self.token, self.name, self.payload)
        response.raise_for_status()
        return SimpleNamespace(data=response.json())


class _PostgrestRpcClient:
    def __init__(self, postgrest_url: str, token: str):
        self.postgrest_url = postgrest_url
        self.token = token

    def rpc(self, name: str, payload: dict):
        return _PostgrestRpcExecution(self.postgrest_url, self.token, name, payload)


@pytest.fixture
def api_client(monkeypatch, postgrest_url, service_token):
    rpc_client = _PostgrestRpcClient(postgrest_url, service_token)
    monkeypatch.setattr(leaderboard, "get_client", lambda: rpc_client)
    leaderboard._cache.clear()
    app = FastAPI()
    app.include_router(leaderboard.router)
    with TestClient(app) as client:
        yield client
    leaderboard._cache.clear()


def _fixture_id(sequence_number: int) -> str:
    digest = hashlib.md5(
        f"b1n431-event-{sequence_number}".encode(), usedforsecurity=False
    ).hexdigest()
    return str(UUID(digest))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@lru_cache(maxsize=1)
def _fixture_rows() -> tuple[dict, ...]:
    default_expiry = int(
        datetime(2026, 4, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    )
    settlement_base = datetime(2026, 3, 31, tzinfo=timezone.utc)
    rows = []
    for sequence_number in range(1, 100_001):
        settled = sequence_number % 5 == 0
        rows.append(
            {
                "id": _fixture_id(sequence_number),
                "user_address": (
                    SCALE_WALLET
                    if sequence_number <= 80_000
                    else f"0x{1000 + sequence_number % 150:040d}"
                ),
                "collateral_usd": 1,
                "premium": 100,
                "net_premium": 100,
                "is_put": sequence_number % 2 == 0,
                "asset": "eth",
                "indexed_at": _iso(
                    START_DT + timedelta(seconds=sequence_number % 100_000)
                ),
                "expiry": default_expiry,
                "is_itm": False if settled else None,
                "settled_at": _iso(
                    settlement_base + timedelta(seconds=sequence_number % 80_000)
                    if settled
                    else None
                ),
            }
        )

    def golden(
        block: int,
        wallet_suffix: int,
        collateral: float,
        net_premium: int | None,
        is_put: bool,
        asset: str,
        indexed_at: str,
        expiry: str,
        is_itm: bool,
        settled_at: str,
        *,
        premium: int | None = None,
    ) -> None:
        row = rows[block - 1]
        row.update(
            {
                "user_address": f"0x{wallet_suffix:040d}",
                "collateral_usd": collateral,
                "premium": net_premium if premium is None else premium,
                "net_premium": net_premium,
                "is_put": is_put,
                "asset": asset,
                "indexed_at": indexed_at,
                "expiry": int(datetime.fromisoformat(expiry).timestamp()),
                "is_itm": is_itm,
                "settled_at": settled_at,
            }
        )

    golden(
        5,
        3,
        250,
        100_000,
        True,
        "eth",
        "2026-04-01T08:00:00+00:00",
        "2026-04-02T23:59:59+00:00",
        True,
        "2026-04-01T10:00:00+00:00",
    )
    golden(
        10,
        3,
        250,
        100_000,
        False,
        "eth",
        "2026-04-01T20:00:00+00:00",
        "2026-04-05T23:59:59+00:00",
        True,
        "2026-04-05T10:00:00+00:00",
    )
    golden(
        15,
        4,
        200,
        100_000,
        True,
        "eth",
        "2026-04-02T08:00:00+00:00",
        "2026-04-02T23:59:59+00:00",
        False,
        "2026-04-03T08:00:00+00:00",
    )
    golden(
        20,
        4,
        200,
        100_000,
        True,
        "eth",
        "2026-04-07T08:00:00+00:00",
        "2026-04-07T23:59:59+00:00",
        False,
        "2026-04-09T08:00:00+00:00",
    )
    golden(
        25,
        4,
        200,
        100_000,
        True,
        "eth",
        "2026-04-08T08:00:00+00:00",
        "2026-04-08T23:59:59+00:00",
        False,
        "2026-04-10T08:00:00+00:00",
    )
    golden(
        30,
        5,
        100,
        50_000,
        True,
        "eth",
        "2026-04-01T00:10:00+00:00",
        "2026-04-01T23:59:59+00:00",
        False,
        "2026-04-01T01:00:00+00:00",
    )
    golden(
        35,
        5,
        100,
        50_000,
        True,
        "eth",
        "2026-04-01T00:20:00+00:00",
        "2026-04-01T23:59:59+00:00",
        False,
        "2026-04-01T02:00:00+00:00",
    )
    golden(
        40,
        5,
        100,
        50_000,
        True,
        "eth",
        "2026-04-01T00:30:00+00:00",
        "2026-04-01T23:59:59+00:00",
        False,
        "2026-04-01T03:00:00+00:00",
    )
    golden(
        45,
        5,
        100,
        50_000,
        True,
        "eth",
        "2026-04-02T00:10:00+00:00",
        "2026-04-02T23:59:59+00:00",
        True,
        "2026-04-02T01:00:00+00:00",
    )
    golden(
        50,
        5,
        100,
        50_000,
        True,
        "eth",
        "2026-04-03T00:10:00+00:00",
        "2026-04-03T23:59:59+00:00",
        False,
        "2026-04-03T01:00:00+00:00",
    )
    golden(
        55,
        5,
        100,
        50_000,
        True,
        "eth",
        "2026-04-03T00:20:00+00:00",
        "2026-04-03T23:59:59+00:00",
        False,
        "2026-04-03T02:00:00+00:00",
    )
    golden(
        60,
        6,
        500,
        100_000,
        True,
        "eth",
        "2026-04-04T08:00:00+00:00",
        "2026-04-04T23:59:59+00:00",
        True,
        "2026-04-04T09:00:00+00:00",
    )
    golden(
        65,
        7,
        500,
        100_000,
        True,
        "eth",
        "2026-04-05T08:00:00+00:00",
        "2026-04-05T23:59:59+00:00",
        True,
        "2026-04-05T09:00:00+00:00",
    )
    golden(
        70,
        8,
        499.996,
        1_000_000,
        True,
        "eth",
        "2026-04-06T08:00:00+00:00",
        "2026-04-06T23:59:59+00:00",
        True,
        "2026-04-06T09:00:00+00:00",
    )
    golden(
        75,
        9,
        125,
        100_000,
        True,
        "eth",
        "2026-04-01T07:00:00+00:00",
        "2026-04-01T23:59:59+00:00",
        True,
        "2026-04-01T08:00:00+00:00",
    )
    golden(
        80,
        9,
        125,
        100_000,
        False,
        "btc",
        "2026-04-01T12:00:00+00:00",
        "2026-04-01T23:59:59+00:00",
        True,
        "2026-04-02T08:00:00+00:00",
    )
    golden(
        85,
        9,
        125,
        100_000,
        True,
        "eth",
        "2026-04-03T07:00:00+00:00",
        "2026-04-03T23:59:59+00:00",
        True,
        "2026-04-03T08:00:00+00:00",
    )
    golden(
        90,
        9,
        125,
        100_000,
        False,
        "eth",
        "2026-04-03T12:00:00+00:00",
        "2026-04-03T23:59:59+00:00",
        False,
        "2026-04-04T08:00:00+00:00",
    )
    golden(
        95,
        10,
        250,
        None,
        True,
        "eth",
        "2026-03-30T00:00:00+00:00",
        "2026-03-30T00:00:00+00:00",
        False,
        "2026-04-12T23:59:58+00:00",
        premium=200_000,
    )
    golden(
        100,
        10,
        250,
        0,
        True,
        "eth",
        "2026-04-12T23:59:59+00:00",
        "2026-04-12T23:59:59+00:00",
        True,
        "2026-04-12T23:59:59+00:00",
        premium=900_000,
    )

    subcent_base = datetime(2026, 4, 11, 12, tzinfo=timezone.utc)
    subcent_expiry = int(
        datetime(2026, 4, 11, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    )
    for block in range(101, 201):
        rows[block - 1].update(
            {
                "user_address": f"0x{2000 + block:040d}",
                "collateral_usd": 0.004,
                "premium": 0,
                "net_premium": 0,
                "indexed_at": _iso(subcent_base + timedelta(seconds=block)),
                "expiry": subcent_expiry,
                "is_itm": None,
                "settled_at": None,
            }
        )
    for offset, (wallet, raw, _expected) in enumerate(ROUNDING_WALLETS, start=1):
        rows[200 + offset - 1].update(
            {
                "user_address": wallet,
                "collateral_usd": raw,
                "premium": 0,
                "net_premium": 0,
                "indexed_at": _iso(ROUNDING_BASE + timedelta(seconds=offset * 10)),
                "expiry": int((ROUNDING_BASE + timedelta(days=1)).timestamp()),
                "is_itm": None,
                "settled_at": None,
            }
        )

    predecessor_rows = (
        (205, PROGRESS_WALLET, 0.075, 0, None),
        (206, SEVEN_UNIT_RANK_WALLETS[0], 501.0, 0, True),
        (207, SEVEN_UNIT_RANK_WALLETS[1], 501.0, 7, False),
        (208, RATE_TIE_WALLETS[0], 501.0, 0, True),
        (209, RATE_TIE_WALLETS[1], 501.0, 167, False),
    )
    for offset, (block, wallet, collateral, premium, is_itm) in enumerate(
        predecessor_rows, start=1
    ):
        rows[block - 1].update(
            {
                "user_address": wallet,
                "collateral_usd": collateral,
                "premium": premium,
                "net_premium": premium,
                "indexed_at": _iso(
                    PREDECESSOR_ROUNDING_BASE + timedelta(seconds=offset * 10)
                ),
                "expiry": int(
                    (PREDECESSOR_ROUNDING_BASE + timedelta(days=1)).timestamp()
                ),
                "is_itm": is_itm,
                "settled_at": (
                    _iso(
                        datetime(2026, 4, 4, 12, tzinfo=timezone.utc)
                        + timedelta(seconds=offset - 1)
                    )
                    if is_itm is not None
                    else None
                ),
            }
        )
    return tuple(rows)


def _static_snapshot(payload: dict) -> dict:
    meta = payload["meta"]
    return {
        "track1": payload["track1"],
        "track2": payload["track2"],
        "meta": {
            "competition_start": meta["competition_start"],
            "competition_end": meta["competition_end"],
            "total_participants": meta["total_participants"],
            "qualified_participants": meta["qualified_participants"],
            "total_volume_usd": meta["total_volume_usd"],
            "limit": meta["limit"],
            "truncated": meta["truncated"],
        },
    }


def _static_me(payload: dict) -> dict:
    return {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "as_of",
            "cache_ttl_seconds",
            "cache_age_seconds",
            "cache_hit",
        }
    }


def test_100k_sql_to_api_snapshot_matches_oracle_with_bounded_rows_bytes_and_time(
    postgrest_url,
    service_token,
    api_client,
):
    count = httpx.get(
        f"{postgrest_url}/order_events",
        headers=_headers(service_token, prefer="count=exact"),
        params={"select": "id", "limit": "1"},
        timeout=30,
    )
    count.raise_for_status()
    assert count.headers["content-range"].endswith("/100000")

    started_at = time.perf_counter()
    response = api_client.get(
        "/leaderboard",
        params={"start": START, "end": END, "limit": 100},
    )
    sql_seconds = time.perf_counter() - started_at
    response.raise_for_status()
    actual = response.json()
    expected = snapshot(_fixture_rows(), start=START, end=END, limit=100)

    assert _static_snapshot(actual) == expected
    assert len(actual["track1"]) == 100
    assert len(actual["track2"]) == 100
    assert actual["meta"]["total_participants"] > 100
    assert actual["meta"]["truncated"] is True
    assert len(response.content) < 128 * 1024
    assert sql_seconds < 30
    assert set(actual["meta"]) == {
        "competition_start",
        "competition_end",
        "total_participants",
        "qualified_participants",
        "total_volume_usd",
        "current_week",
        "limit",
        "truncated",
        "as_of",
        "cache_ttl_seconds",
        "cache_age_seconds",
        "cache_hit",
    }
    as_of = datetime.fromisoformat(actual["meta"]["as_of"].replace("Z", "+00:00"))
    assert as_of.tzinfo is not None
    assert actual["meta"]["cache_ttl_seconds"] <= 60
    print(
        "B1N-431 bounded 100k evidence: "
        f"source_rows=100000 track1_rows={len(actual['track1'])} "
        f"track2_rows={len(actual['track2'])} response_bytes={len(response.content)} "
        f"sql_seconds={sql_seconds:.3f}"
    )


def test_real_sql_to_api_snapshot_covers_named_golden_semantics(api_client):
    response = api_client.get(
        "/leaderboard",
        params={"start": START, "end": END, "limit": 100},
    )
    response.raise_for_status()
    by_wallet = {row["wallet"]: row for row in response.json()["track1"]}

    assert by_wallet["0x0000000000000000000000000000000000000003"]["wheel_count"] == 1
    assert (
        by_wallet["0x0000000000000000000000000000000000000003"]["total_earned_usd"]
        == 0.3
    )
    assert (
        by_wallet["0x0000000000000000000000000000000000000004"]["total_earned_usd"]
        == 0.45
    )
    assert (
        by_wallet["0x0000000000000000000000000000000000000005"]["total_earned_usd"]
        == 0.3
    )
    assert by_wallet["0x0000000000000000000000000000000000000005"]["qualified"] is True
    below_threshold = by_wallet["0x0000000000000000000000000000000000000008"]
    assert below_threshold["total_collateral_usd"] == 500.0
    assert below_threshold["qualified"] is False
    exact_threshold = by_wallet["0x0000000000000000000000000000000000000006"]
    assert exact_threshold["total_collateral_usd"] == 500.0
    assert exact_threshold["qualified"] is True
    assert by_wallet["0x0000000000000000000000000000000000000009"]["wheel_count"] == 0
    assert by_wallet[FALLBACK_WALLET]["total_earned_usd"] == 0.2
    ordered = [row["wallet"] for row in response.json()["track1"]]
    assert ordered.index("0x0000000000000000000000000000000000000006") < ordered.index(
        "0x0000000000000000000000000000000000000007"
    )
    streaks = {row["wallet"]: row["otm_streak"] for row in response.json()["track2"]}
    assert streaks["0x0000000000000000000000000000000000000005"] == 3


def test_subcent_wallets_round_only_after_raw_metadata_sum(api_client):
    response = api_client.get(
        "/leaderboard",
        params={"start": SUBCENT_START, "end": SUBCENT_END, "limit": 100},
    )
    response.raise_for_status()
    actual = response.json()
    expected = snapshot(
        _fixture_rows(), start=SUBCENT_START, end=SUBCENT_END, limit=100
    )

    assert _static_snapshot(actual) == expected
    assert actual["meta"]["total_participants"] == 100
    assert actual["meta"]["total_volume_usd"] == 0.4
    assert sum(row["total_collateral_usd"] for row in actual["track1"]) == 0
    assert all(row["qualified"] is False for row in actual["track1"])


def test_arbitrary_range_and_inclusive_boundaries_match_oracle(api_client):
    arbitrary_start = int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp())
    arbitrary_end = int(
        datetime(2026, 4, 5, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    )
    for start, end in ((START, END), (arbitrary_start, arbitrary_end)):
        response = api_client.get(
            "/leaderboard",
            params={"start": start, "end": end, "limit": 100},
        )
        response.raise_for_status()
        assert _static_snapshot(response.json()) == snapshot(
            _fixture_rows(), start=start, end=end, limit=100
        )

    full = api_client.get(
        "/leaderboard/me",
        params={"address": FALLBACK_WALLET, "start": START, "end": END},
    )
    full.raise_for_status()
    assert full.json()["position_count"] == 2


def test_me_is_fixed_cardinality_for_nonempty_and_empty_wallets(api_client):
    for address in (SCALE_WALLET, FALLBACK_WALLET, EMPTY_WALLET):
        response = api_client.get(
            "/leaderboard/me",
            params={"address": address, "start": START, "end": END},
        )
        response.raise_for_status()
        body = response.json()
        assert isinstance(body, dict)
        assert len(response.content) < 4 * 1024
        assert set(body) == {
            "wallet",
            "position_count",
            "total_collateral_usd",
            "total_earned_usd",
            "earning_rate",
            "active_days",
            "wheel_count",
            "otm_streak",
            "qualifies",
            "as_of",
            "cache_ttl_seconds",
            "cache_age_seconds",
            "cache_hit",
        }
        assert _static_me(body) == wallet_me(
            _fixture_rows(), address=address, start=START, end=END
        )
        assert datetime.fromisoformat(body["as_of"].replace("Z", "+00:00")).tzinfo
        assert body["cache_ttl_seconds"] <= 60


def test_direct_rpcs_expose_only_clearly_internal_raw_rounding_fields(
    postgrest_url, service_token
):
    board = _rpc(
        postgrest_url,
        service_token,
        "b1nary_legacy_leaderboard",
        {"p_start": START, "p_end": END, "p_limit": 1},
    )
    board.raise_for_status()
    board_body = board.json()
    assert "_internal_raw_collateral_usd" in board_body["track1"][0]
    assert "total_collateral_usd" not in board_body["track1"][0]
    assert "_internal_raw_total_volume_usd" in board_body["meta"]
    assert "total_volume_usd" not in board_body["meta"]

    personal = _rpc(
        postgrest_url,
        service_token,
        "b1nary_legacy_leaderboard_me",
        {
            "p_address": ROUNDING_WALLETS[0][0],
            "p_start": START,
            "p_end": END,
        },
    )
    personal.raise_for_status()
    assert personal.json()["_internal_raw_collateral_usd"] == 2.675
    assert "total_collateral_usd" not in personal.json()


@pytest.mark.parametrize(
    ("offset", "wallet", "raw", "expected"),
    [(offset, *case) for offset, case in enumerate(ROUNDING_WALLETS, start=1)],
)
def test_real_postgres_to_api_preserves_python_half_cent_wire_semantics(
    api_client, offset, wallet, raw, expected
):
    event_second = int((ROUNDING_BASE + timedelta(seconds=offset * 10)).timestamp())
    start = event_second - 1
    end = event_second

    board = api_client.get(
        "/leaderboard", params={"start": start, "end": end, "limit": 1}
    )
    personal = api_client.get(
        "/leaderboard/me",
        params={"address": wallet, "start": start, "end": end},
    )

    board.raise_for_status()
    personal.raise_for_status()
    assert board.json()["track1"][0]["wallet"] == wallet
    assert board.json()["track1"][0]["total_collateral_usd"] == expected
    assert board.json()["meta"]["total_volume_usd"] == expected
    assert personal.json()["total_collateral_usd"] == expected
    assert round(raw, 2) == expected
    assert "_internal_raw" not in board.text
    assert "_internal_raw" not in personal.text


def test_real_postgres_progress_premium_earning_and_rank_match_python(api_client):
    progress_second = int(
        (PREDECESSOR_ROUNDING_BASE + timedelta(seconds=10)).timestamp()
    )
    progress = api_client.get(
        "/leaderboard",
        params={"start": progress_second - 1, "end": progress_second, "limit": 1},
    )
    progress.raise_for_status()
    progress_entry = progress.json()["track1"][0]
    assert progress_entry["wallet"] == PROGRESS_WALLET
    assert progress_entry["progress"]["collateral_pct"] == 0.0001

    seven_start = int((PREDECESSOR_ROUNDING_BASE + timedelta(seconds=19)).timestamp())
    seven_end = int((PREDECESSOR_ROUNDING_BASE + timedelta(seconds=30)).timestamp())
    seven_response = api_client.get(
        "/leaderboard", params={"start": seven_start, "end": seven_end, "limit": 2}
    )
    seven_response.raise_for_status()
    seven_track = seven_response.json()["track1"]
    assert [row["wallet"] for row in seven_track] == list(SEVEN_UNIT_RANK_WALLETS)
    assert [row["rank"] for row in seven_track] == [1, 2]
    assert [row["earning_rate"] for row in seven_track] == [0.0, 0.0]
    assert seven_track[1]["total_earned_usd"] == 0.000010
    assert _static_snapshot(seven_response.json()) == snapshot(
        _fixture_rows(), start=seven_start, end=seven_end, limit=2
    )

    rate_start = int((PREDECESSOR_ROUNDING_BASE + timedelta(seconds=39)).timestamp())
    rate_end = int((PREDECESSOR_ROUNDING_BASE + timedelta(seconds=50)).timestamp())
    rate_response = api_client.get(
        "/leaderboard", params={"start": rate_start, "end": rate_end, "limit": 2}
    )
    rate_response.raise_for_status()
    track = rate_response.json()["track1"]
    assert [row["wallet"] for row in track] == list(RATE_TIE_WALLETS)
    assert [row["rank"] for row in track] == [1, 2]
    assert [row["earning_rate"] for row in track] == [0.0, 0.0]
    assert track[1]["total_earned_usd"] == 0.000250
    assert _static_snapshot(rate_response.json()) == snapshot(
        _fixture_rows(), start=rate_start, end=rate_end, limit=2
    )


def test_private_round_helper_matches_python_over_broad_binary_boundary_matrix(
    postgrest_url, service_token
):
    total_cases = 0
    known_values = {
        4: [0.00015, 0.00025, 0.075 / 500, 1.23445, 1.23455],
        6: [0.0000105, 0.0000115, 0.00025049999999999996, 0.0000005],
    }
    for ndigits in (4, 6):
        scale = 10**ndigits
        boundaries = [(integer + 0.5) / scale for integer in range(-2048, 2049, 8)]
        boundaries.extend(
            (integer + 0.5) / scale
            for integer in (-(10**9), -(10**6), -(10**4), 10**4, 10**6, 10**9)
        )
        values = list(known_values[ndigits])
        for boundary in boundaries:
            values.extend(
                (
                    math.nextafter(boundary, -math.inf),
                    boundary,
                    math.nextafter(boundary, math.inf),
                )
            )

        cases = [{"value": value, "ndigits": ndigits} for value in values]
        response = _rpc(
            postgrest_url,
            service_token,
            "b1n431_fixture_python_round",
            {"p_cases": cases},
        )
        response.raise_for_status()
        actual = response.json()
        expected = [round(float(value), ndigits) for value in values]
        assert actual == expected
        total_cases += len(cases)

    assert total_cases > 3_000
    assert round(0.00015, 4) == 0.0001
    assert round(0.00025, 4) == 0.0003
    assert round(0.0000105, 6) == 0.000010
    assert round(0.0000115, 6) == 0.000012
    print(f"B1N-431 Python-round oracle evidence: cases={total_cases} ndigits=4,6")


def test_rpcs_are_service_only_and_private_helper_is_not_exposed(
    postgrest_url,
    service_token,
):
    anon = _required_env("B1N431_ANON_TOKEN")
    authenticated = _required_env("B1N431_AUTHENTICATED_TOKEN")
    calls = [
        (
            "b1nary_legacy_leaderboard",
            {"p_start": START, "p_end": END, "p_limit": 1},
        ),
        (
            "b1nary_legacy_leaderboard_me",
            {"p_address": FALLBACK_WALLET, "p_start": START, "p_end": END},
        ),
    ]
    for name, payload in calls:
        assert _rpc(postgrest_url, anon, name, payload).status_code == 401
        assert _rpc(postgrest_url, authenticated, name, payload).status_code == 403
        assert _rpc(postgrest_url, service_token, name, payload).status_code == 200

    private_helper = _rpc(
        postgrest_url,
        service_token,
        "b1n431_wallet_stats",
        {"p_rows": []},
    )
    assert private_helper.status_code == 404


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    [
        (
            "b1nary_legacy_leaderboard",
            {"p_start": START, "p_end": END, "p_limit": None},
            "limit must be between 1 and 100",
        ),
        (
            "b1nary_legacy_leaderboard",
            {"p_start": None, "p_end": END, "p_limit": 1},
            "start must be before end",
        ),
        (
            "b1nary_legacy_leaderboard_me",
            {"p_address": None, "p_start": START, "p_end": END},
            "invalid Ethereum address",
        ),
    ],
)
def test_rpc_nulls_cannot_bypass_bounds(
    postgrest_url, service_token, name, payload, message
):
    response = _rpc(postgrest_url, service_token, name, payload)
    assert response.status_code == 400
    assert message in response.text


def test_natural_source_window_and_wallet_indexes_are_used(
    postgrest_url, service_token
):
    response = _rpc(
        postgrest_url,
        service_token,
        "b1n431_fixture_query_plans",
        {},
    )
    response.raise_for_status()
    plans = response.json()
    global_plan = json.dumps(plans["global"])
    wallet_plan = json.dumps(plans["wallet"])
    source_fields = {
        "id",
        "collateral_usd",
        "net_premium",
        "premium",
        "is_put",
        "asset",
        "indexed_at",
        "expiry",
        "is_itm",
        "settled_at",
    }
    assert "jsonb_agg" in global_plan
    assert "jsonb_agg" in wallet_plan
    assert all(field in global_plan for field in source_fields)
    assert all(field in wallet_plan for field in source_fields)
    assert "idx_order_events_indexed_user_id" in global_plan
    assert "idx_b1n431_order_events_user_indexed_id" in wallet_plan
    assert "Index Only Scan" not in global_plan
    assert "Index Only Scan" not in wallet_plan
    print(f"B1N-431 global natural plan: {global_plan}")
    print(f"B1N-431 wallet natural plan: {wallet_plan}")
