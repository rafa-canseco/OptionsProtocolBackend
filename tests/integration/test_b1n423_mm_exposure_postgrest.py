import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from unittest.mock import patch

import httpx
import jwt
import pytest
from postgrest import SyncPostgrestClient


pytestmark = [pytest.mark.integration, pytest.mark.network]

MM_ADDRESS = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_MM_ADDRESS = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OTOKEN_ADDRESS = "0xcccccccccccccccccccccccccccccccccccccccc"
NOW_TS = 1_000
RPC_FIELDS = {
    "active_quotes_count",
    "active_quotes_notional",
    "open_positions_by_expiry",
    "total_premium_earned",
    "pending_settlement_count",
}


@dataclass
class _PostgrestContext:
    base_url: str
    client: httpx.Client = field(repr=False)
    service_token: str = field(repr=False)
    authenticated_token: str = field(repr=False)


@pytest.fixture(scope="module")
def postgrest_context():
    base_url = os.getenv("B1N423_POSTGREST_URL")
    signing_value = os.getenv("B1N423_JWT_SIGNING_VALUE")
    if not base_url or not signing_value:
        pytest.skip("run scripts/test-b1n423-postgrest.sh to provision the fixture")

    now = int(time.time())

    def token(role: str) -> str:
        return jwt.encode(
            {"role": role, "iat": now, "exp": now + 300},
            signing_value,
            algorithm="HS256",
        )

    service_token = token("service_role")
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {service_token}"},
        timeout=10,
    ) as client:
        yield _PostgrestContext(
            base_url=base_url,
            client=client,
            service_token=service_token,
            authenticated_token=token("authenticated"),
        )


def _json(response: httpx.Response):
    return json.loads(response.text, parse_float=Decimal, parse_int=int)


def _assert_success(response: httpx.Response) -> None:
    assert response.is_success, f"{response.status_code}: {response.text}"


def _reset(client: httpx.Client) -> None:
    for table in ("order_events", "mm_quotes"):
        response = client.delete(f"/{table}", headers={"Prefer": "return=minimal"})
        _assert_success(response)


def _insert(client: httpx.Client, table: str, rows: list[dict]) -> None:
    response = client.post(f"/{table}", json=rows, headers={"Prefer": "return=minimal"})
    _assert_success(response)


def _rpc(client: httpx.Client) -> httpx.Response:
    return client.post(
        "/rpc/v1_get_mm_exposure",
        json={"p_mm_address": MM_ADDRESS, "p_now_ts": NOW_TS},
    )


def _rpc_row(client: httpx.Client) -> dict:
    response = _rpc(client)
    _assert_success(response)
    payload = _json(response)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert set(payload[0]) == RPC_FIELDS
    return payload[0]


def _read_legacy_inputs(client: httpx.Client) -> tuple[list[dict], list[dict]]:
    quotes_response = client.get(
        "/mm_quotes",
        params={
            "select": "max_amount",
            "mm_address": f"eq.{MM_ADDRESS}",
            "is_active": "is.true",
            "deadline": f"gt.{NOW_TS}",
        },
    )
    fills_response = client.get(
        "/order_events",
        params={
            "select": "amount,premium,gross_premium,expiry,is_settled",
            "mm_address": f"eq.{MM_ADDRESS}",
        },
    )
    _assert_success(quotes_response)
    _assert_success(fills_response)
    return _json(quotes_response), _json(fills_response)


def _legacy_exposure(quotes: list[dict], fills: list[dict]) -> dict:
    expiry_buckets: dict[int, dict] = defaultdict(
        lambda: {"count": 0, "amount": Decimal("0")}
    )
    total_premium = Decimal("0")
    pending_settlement = 0

    for fill in fills:
        premium = fill.get("gross_premium") or fill.get("premium", 0)
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


def _quote(
    quote_id: str,
    *,
    max_amount: str,
    deadline: int = 2_000,
    is_active: bool = True,
    mm_address: str = MM_ADDRESS,
) -> dict:
    return {
        "mm_address": mm_address,
        "otoken_address": OTOKEN_ADDRESS,
        "bid_price": "1.25",
        "deadline": deadline,
        "quote_id": quote_id,
        "max_amount": max_amount,
        "maker_nonce": 1,
        "signature": f"signature-{quote_id}",
        "is_active": is_active,
    }


def _fill(
    index: int,
    *,
    amount: str,
    premium: str,
    gross_premium: str | None,
    expiry: int | None,
    is_settled: bool | None,
    mm_address: str = MM_ADDRESS,
) -> dict:
    return {
        "tx_hash": f"0xtx{index:02d}",
        "block_number": index,
        "log_index": index,
        "chain": "base",
        "user_address": "0xdddddddddddddddddddddddddddddddddddddddd",
        "mm_address": mm_address,
        "otoken_address": OTOKEN_ADDRESS,
        "amount": amount,
        "premium": premium,
        "gross_premium": gross_premium,
        "collateral": "1",
        "vault_id": index,
        "expiry": expiry,
        "is_settled": is_settled,
    }


def _api_exposure(base_url: str, token: str) -> dict:
    # Importing the route initializes Settings. Supply explicit local placeholders
    # so this opt-in test never reads a developer .env or remote Supabase project.
    os.environ.setdefault("SUPABASE_" + "URL", base_url)
    os.environ.setdefault("SUPABASE_" + "ANON_KEY", "local-integration-placeholder")
    os.environ.setdefault(
        "SUPABASE_" + "SERVICE_ROLE_KEY", "local-integration-placeholder"
    )
    from src.api.mm_routes import get_exposure

    client = SyncPostgrestClient(base_url)
    client.auth(token)
    try:
        with (
            patch("src.api.mm_routes.get_client", return_value=client),
            patch("src.api.mm_routes.time.time", return_value=NOW_TS),
        ):
            return asyncio.run(get_exposure(mm_address=MM_ADDRESS)).model_dump()
    finally:
        client.aclose()


def test_rpc_roles_and_empty_shape(postgrest_context: _PostgrestContext) -> None:
    client = postgrest_context.client
    _reset(client)

    assert _rpc_row(client) == {
        "active_quotes_count": 0,
        "active_quotes_notional": "0",
        "open_positions_by_expiry": [],
        "total_premium_earned": "0",
        "pending_settlement_count": 0,
    }

    with httpx.Client(base_url=postgrest_context.base_url, timeout=10) as anon:
        anon_response = _rpc(anon)
    with httpx.Client(
        base_url=postgrest_context.base_url,
        headers={"Authorization": f"Bearer {postgrest_context.authenticated_token}"},
        timeout=10,
    ) as authenticated:
        authenticated_response = _rpc(authenticated)

    assert anon_response.status_code == 401
    assert authenticated_response.status_code == 403

    assert _api_exposure(
        postgrest_context.base_url, postgrest_context.service_token
    ) == {
        "active_quotes_count": 0,
        "active_quotes_notional": "0",
        "open_positions_by_expiry": [],
        "total_premium_earned": "0",
        "pending_settlement_count": 0,
    }


def test_rpc_matches_legacy_semantics_for_mixed_numeric_and_null_history(
    postgrest_context: _PostgrestContext,
) -> None:
    client = postgrest_context.client
    _reset(client)

    _insert(
        client,
        "mm_quotes",
        [
            _quote(
                "active-large",
                max_amount="90071992547409931234.1234567",
            ),
            _quote("active-small", max_amount="0.0000001"),
            _quote("deadline-equal", max_amount="17", deadline=NOW_TS),
            _quote("inactive", max_amount="19", is_active=False),
            _quote(
                "other-mm",
                max_amount="23",
                mm_address=OTHER_MM_ADDRESS,
            ),
        ],
    )
    _insert(
        client,
        "order_events",
        [
            _fill(
                1,
                amount="90071992547409931234.1234567",
                premium="1",
                gross_premium="80071992547409931234.1234567",
                expiry=1_500,
                is_settled=False,
            ),
            _fill(
                2,
                amount="0.0000001",
                premium="2.0000001",
                gross_premium="0",
                expiry=1_500,
                is_settled=True,
            ),
            _fill(
                3,
                amount="2.5",
                premium="3.5",
                gross_premium=None,
                expiry=2_000,
                is_settled=None,
            ),
            _fill(
                4,
                amount="4",
                premium="7",
                gross_premium=None,
                expiry=NOW_TS,
                is_settled=False,
            ),
            _fill(
                5,
                amount="5",
                premium="11",
                gross_premium="13",
                expiry=900,
                is_settled=True,
            ),
            _fill(
                6,
                amount="6",
                premium="17",
                gross_premium="19",
                expiry=800,
                is_settled=None,
            ),
            _fill(
                7,
                amount="7",
                premium="23",
                gross_premium="0",
                expiry=0,
                is_settled=False,
            ),
            _fill(
                8,
                amount="8",
                premium="29",
                gross_premium=None,
                expiry=None,
                is_settled=False,
            ),
            _fill(
                9,
                amount="31",
                premium="31",
                gross_premium="31",
                expiry=700,
                is_settled=False,
                mm_address=OTHER_MM_ADDRESS,
            ),
        ],
    )

    quotes, fills = _read_legacy_inputs(client)
    expected = _legacy_exposure(quotes, fills)
    assert _rpc_row(client) == expected

    assert (
        _api_exposure(postgrest_context.base_url, postgrest_context.service_token)
        == expected
    )
