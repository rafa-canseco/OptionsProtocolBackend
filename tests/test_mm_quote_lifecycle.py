"""Lifecycle enrichment for GET /mm/quotes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api.mm_routes import (
    _MM_OTOKEN_LIFECYCLE_SELECT,
    _MM_QUOTE_SELECT,
    get_quotes,
)


def _quote(address: str, index: int) -> dict:
    return {
        "id": f"quote-{index}",
        "otoken_address": address,
        "bid_price": "1250000",
        "deadline": 2_000_000_000,
        "quote_id": str(index),
        "max_amount": "100000000",
        "maker_nonce": 3,
        "signature": "0x" + ("ab" * 65),
        "asset": "eth",
        "strike_price": 2500,
        "expiry": 2_000_100_000,
        "is_put": False,
        "is_active": True,
        "created_at": "2033-05-18T03:33:20+00:00",
    }


class _Query:
    def __init__(self, client: "_Client", table_name: str):
        self.client = client
        self.table_name = table_name

    def select(self, columns: str):
        self.client.selects.append((self.table_name, columns))
        return self

    def eq(self, *_args):
        return self

    def gt(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def in_(self, column: str, values: list[str]):
        self.client.in_filters.append((self.table_name, column, values))
        return self

    def execute(self):
        payload = self.client.payloads[self.table_name]
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(data=payload)


class _Client:
    def __init__(self, quotes: list[dict], lifecycle_rows):
        self.payloads = {
            "mm_quotes": quotes,
            "available_otokens": lifecycle_rows,
        }
        self.table_calls: list[str] = []
        self.selects: list[tuple[str, str]] = []
        self.in_filters: list[tuple[str, str, list[str]]] = []

    def table(self, table_name: str):
        self.table_calls.append(table_name)
        return _Query(self, table_name)


def _get_quotes(client: _Client):
    with patch("src.api.mm_routes.get_client", return_value=client):
        return asyncio.run(
            get_quotes(mm_address="0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa")
        )


def test_get_quotes_enriches_all_lifecycle_states_in_one_batch_query():
    addresses = [f"0x{index:040x}" for index in range(1, 6)]
    quotes = [_quote(address, index) for index, address in enumerate(addresses)]
    lifecycle_rows = [
        {"otoken_address": addresses[0], "deployment_status": "virtual"},
        {"otoken_address": addresses[1], "deployment_status": "creating"},
        {"otoken_address": addresses[2], "deployment_status": "failed"},
        {"otoken_address": addresses[3], "deployment_status": "ready"},
    ]
    client = _Client(quotes, lifecycle_rows)

    result = _get_quotes(client)

    assert [quote.deployment_status for quote in result] == [
        "virtual",
        "creating",
        "failed",
        "ready",
        "ready",
    ]
    assert client.table_calls == ["mm_quotes", "available_otokens"]
    assert client.selects == [
        ("mm_quotes", _MM_QUOTE_SELECT),
        ("available_otokens", _MM_OTOKEN_LIFECYCLE_SELECT),
    ]
    assert client.in_filters == [
        ("available_otokens", "otoken_address", sorted(addresses))
    ]


@pytest.mark.parametrize("invalid_status", [None, "", "pending", "READY"])
def test_known_series_with_missing_or_invalid_status_fails_closed(invalid_status):
    address = "0x" + ("ab" * 20)
    lifecycle_row = {"otoken_address": address}
    if invalid_status is not None:
        lifecycle_row["deployment_status"] = invalid_status
    client = _Client([_quote(address, 1)], [lifecycle_row])

    result = _get_quotes(client)

    assert result[0].deployment_status == "failed"


def test_quote_without_lifecycle_row_preserves_legacy_ready_status():
    address = "0x" + ("cd" * 20)
    client = _Client([_quote(address, 1)], [])

    result = _get_quotes(client)

    assert result[0].deployment_status == "ready"


def test_empty_quote_result_skips_lifecycle_query():
    client = _Client([], [])

    result = _get_quotes(client)

    assert result == []
    assert client.table_calls == ["mm_quotes"]


@pytest.mark.parametrize(
    "lifecycle_payload",
    [None, RuntimeError("registry unavailable")],
)
def test_lifecycle_lookup_failure_does_not_fall_back_to_ready(lifecycle_payload):
    address = "0x" + ("ef" * 20)
    client = _Client([_quote(address, 1)], lifecycle_payload)

    with pytest.raises(HTTPException) as exc_info:
        _get_quotes(client)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Could not fetch quote lifecycle"
