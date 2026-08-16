import asyncio
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


def test_exposure_uses_one_rpc_row_and_preserves_exact_values() -> None:
    response = _get_exposure(
        [
            {
                "active_quotes_count": 2,
                "active_quotes_notional": "90071992547409931234.1234567",
                "open_positions_by_expiry": [
                    {
                        "expiry": 2_000,
                        "position_count": 1,
                        "total_amount": "0.0000001",
                    },
                    {
                        "expiry": 1_500,
                        "position_count": 2,
                        "total_amount": "3.25",
                    },
                ],
                "total_premium_earned": "80071992547409931234.1234567",
                "pending_settlement_count": 3,
            }
        ]
    )

    assert response == {
        "active_quotes_count": 2,
        "active_quotes_notional": str(Decimal("90071992547409931234.1234567")),
        "open_positions_by_expiry": [
            {"expiry": 1_500, "position_count": 2, "total_amount": "3.25"},
            {"expiry": 2_000, "position_count": 1, "total_amount": "1E-7"},
        ],
        "total_premium_earned": str(Decimal("80071992547409931234.1234567")),
        "pending_settlement_count": 3,
    }


def test_exposure_accepts_empty_aggregate_row() -> None:
    assert _get_exposure(
        [
            {
                "active_quotes_count": 0,
                "active_quotes_notional": "0",
                "open_positions_by_expiry": [],
                "total_premium_earned": "0",
                "pending_settlement_count": 0,
            }
        ]
    ) == {
        "active_quotes_count": 0,
        "active_quotes_notional": "0",
        "open_positions_by_expiry": [],
        "total_premium_earned": "0",
        "pending_settlement_count": 0,
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        [{}],
        [{}, {}],
        [{"open_positions_by_expiry": {}}],
        [
            {
                "active_quotes_count": 0,
                "active_quotes_notional": "0",
                "open_positions_by_expiry": [{}] * 101,
                "total_premium_earned": "0",
                "pending_settlement_count": 0,
            }
        ],
    ],
)
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
