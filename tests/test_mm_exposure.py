import asyncio
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api.mm_routes import get_exposure


MM_ADDRESS = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW_TS = 1_000


class _RpcQuery:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


class _RpcClient:
    def __init__(self, data):
        self.data = data
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name: str, params: dict):
        self.calls.append((name, params))
        return _RpcQuery(self.data)

    def table(self, _name: str):
        raise AssertionError("GET /mm/exposure must not transfer table rows")


def _get_exposure(data):
    client = _RpcClient(data)
    with (
        patch("src.api.mm_routes.get_client", return_value=client),
        patch("src.api.mm_routes.time.time", return_value=NOW_TS),
    ):
        response = asyncio.run(get_exposure(mm_address=MM_ADDRESS))
    assert client.calls == [
        (
            "v1_get_mm_exposure",
            {"p_mm_address": MM_ADDRESS.lower(), "p_now_ts": NOW_TS},
        )
    ]
    return response.model_dump()


def _legacy_exposure(quotes: list[dict], fills: list[dict]) -> dict:
    expiry_buckets: dict[int, dict] = defaultdict(
        lambda: {"count": 0, "amount": Decimal("0")}
    )
    total_premium = Decimal("0")
    pending_settlement = 0

    for fill in fills:
        premium = fill.get("gross_premium") or fill.get("premium", "0")
        total_premium += Decimal(str(premium))
        expiry = fill.get("expiry")
        if expiry and expiry > NOW_TS:
            expiry_buckets[expiry]["count"] += 1
            expiry_buckets[expiry]["amount"] += Decimal(str(fill["amount"]))
        if expiry and expiry <= NOW_TS and not fill.get("is_settled"):
            pending_settlement += 1

    return {
        "active_quotes_count": len(quotes),
        "active_quotes_notional": str(
            sum(Decimal(str(quote["max_amount"])) for quote in quotes)
        ),
        "open_positions_by_expiry": [
            {
                "expiry": expiry,
                "position_count": bucket["count"],
                "total_amount": str(bucket["amount"]),
            }
            for expiry, bucket in sorted(expiry_buckets.items())
        ],
        "total_premium_earned": str(total_premium),
        "pending_settlement_count": pending_settlement,
    }


@pytest.mark.parametrize(
    ("quotes", "fills", "rpc_row"),
    [
        (
            [],
            [],
            {
                "active_quotes_count": 0,
                "active_quotes_notional": 0,
                "open_positions_by_expiry": [],
                "total_premium_earned": 0,
                "pending_settlement_count": 0,
            },
        ),
        (
            [{"max_amount": "10"}, {"max_amount": "5.5"}],
            [
                {
                    "expiry": 1_500,
                    "amount": "1.25",
                    "gross_premium": "2",
                    "premium": "2",
                    "is_settled": False,
                },
                {
                    "expiry": 1_500,
                    "amount": "2",
                    "gross_premium": "2.5",
                    "premium": "2.5",
                    "is_settled": False,
                },
            ],
            {
                "active_quotes_count": 2,
                "active_quotes_notional": "15.5",
                "open_positions_by_expiry": [
                    {"expiry": 1_500, "position_count": 2, "total_amount": "3.25"}
                ],
                "total_premium_earned": "4.5",
                "pending_settlement_count": 0,
            },
        ),
        (
            [],
            [
                {
                    "expiry": 900,
                    "amount": 1,
                    "gross_premium": 5,
                    "premium": 5,
                    "is_settled": True,
                }
            ],
            {
                "active_quotes_count": 0,
                "active_quotes_notional": 0,
                "open_positions_by_expiry": [],
                "total_premium_earned": 5,
                "pending_settlement_count": 0,
            },
        ),
        (
            [],
            [
                {
                    "expiry": NOW_TS,
                    "amount": 1,
                    "gross_premium": None,
                    "premium": 7,
                    "is_settled": False,
                }
            ],
            {
                "active_quotes_count": 0,
                "active_quotes_notional": 0,
                "open_positions_by_expiry": [],
                "total_premium_earned": 7,
                "pending_settlement_count": 1,
            },
        ),
        (
            [{"max_amount": 10}, {"max_amount": 5}, {"max_amount": 7}],
            [
                {
                    "expiry": 1_500,
                    "amount": 1,
                    "gross_premium": 2,
                    "premium": 2,
                    "is_settled": False,
                },
                {
                    "expiry": 1_500,
                    "amount": 0.5,
                    "gross_premium": 0,
                    "premium": 3,
                    "is_settled": False,
                },
                {
                    "expiry": 2_000,
                    "amount": 2,
                    "gross_premium": 4,
                    "premium": 4,
                    "is_settled": True,
                },
                {
                    "expiry": 900,
                    "amount": 1,
                    "gross_premium": 1,
                    "premium": 1,
                    "is_settled": False,
                },
                {
                    "expiry": 800,
                    "amount": 1,
                    "gross_premium": 1,
                    "premium": 1,
                    "is_settled": True,
                },
            ],
            {
                "active_quotes_count": 3,
                "active_quotes_notional": "22",
                "open_positions_by_expiry": [
                    {"expiry": 2_000, "position_count": 1, "total_amount": 2},
                    {"expiry": 1_500, "position_count": 2, "total_amount": 1.5},
                ],
                "total_premium_earned": "11",
                "pending_settlement_count": 1,
            },
        ),
    ],
    ids=["empty", "active", "settled", "expired-unsettled", "mixed"],
)
def test_exposure_preserves_all_history_classes(
    quotes: list[dict], fills: list[dict], rpc_row: dict
) -> None:
    assert _get_exposure([rpc_row]) == _legacy_exposure(quotes, fills)


@pytest.mark.parametrize("payload", [None, [], [{}], [{}, {}]])
def test_exposure_fails_closed_on_invalid_rpc_payload(payload) -> None:
    client = _RpcClient(payload)
    with (
        patch("src.api.mm_routes.get_client", return_value=client),
        patch("src.api.mm_routes.time.time", return_value=NOW_TS),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(get_exposure(mm_address=MM_ADDRESS))

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Could not fetch exposure"
