from unittest.mock import patch

import pytest

from src.api import mm_ws
from src.bots import solana_event_indexer as sei

_EXECUTE_DATA = (
    "SofnBahqwnUBsQB2Umg4/K/KmFnUnc10sAKnNPDz+lSGrO1qA1dM4k65tOsUGdaebnZpVyODLISsSIwrQLOSBjasbaVmaKAzrYSduniptY3lTtWgBMK2fs8v0OsSevrprOUH/OHeFuZh/3IKAAAAAGj8/AIAAAAAdJkeAAAAAAC2hgEAAAAAAA=="
)
_DEPOSIT_DATA = (
    "9D5NC4dwPWCkR7tWL7/wiqpHkS0EF3T9Av+d6TezAtS4UllaJEiofAEAAAAAAAAAj3jUjO+NvpGhwQD4wjUGsG10/10zRLyg2hXuG0LXnQF/v3YIAAAAAA=="
)


def test_parse_log_payloads_decodes_real_execute_order_payload():
    logs = [
        "Program log: Instruction: DepositCollateral",
        f"Program data: {_DEPOSIT_DATA}",
        "Program log: Instruction: ExecuteOrder",
        f"Program data: {_EXECUTE_DATA}",
    ]

    row = sei._parse_log_payloads(logs)

    assert row == {
        "user_address": "7bx3QgnwiKn3iQAzqKHpsotg9iC6SGghoUakPRxkkjj",
        "mm_address": "6JK3LrBvjJaKwCuaJPyg7S4NTHvk2Nx7rBpU4YViBa8S",
        "otoken_address": "CgLpT3eudCNR8maFw1LpUoYUL1DuB6ot9bfFsDd9y4jw",
        "amount": "175308641",
        "premium": "50134120",
        "gross_premium": "50134120",
        "net_premium": "2005364",
        "protocol_fee": "100022",
        "vault_id": 1,
        "collateral": "141999999",
    }


def test_build_order_row_sets_chain_and_metadata():
    tx = {
        "meta": {
            "err": None,
            "logMessages": [
                "Program log: Instruction: DepositCollateral",
                f"Program data: {_DEPOSIT_DATA}",
                "Program log: Instruction: ExecuteOrder",
                f"Program data: {_EXECUTE_DATA}",
            ],
        }
    }

    with patch.object(
        sei,
        "_load_otoken_metadata",
        return_value={
            "strike_price": 2030000000,
            "expiry": 1776470400,
            "is_put": True,
            "asset": "sol",
        },
    ):
        row = sei._build_order_row("sig123", 455096803, tx)

    assert row is not None
    assert row["tx_hash"] == "sig123"
    assert row["block_number"] == 455096803
    assert row["log_index"] == 0
    assert row["chain"] == "solana"
    assert row["vault_id"] == 1
    assert row["asset"] == "sol"
    assert row["strike_price"] == 2030000000


class _FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_text(self, payload: str) -> None:
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_notify_mm_fill_preserves_solana_pubkey_case():
    ws = _FakeWebSocket()
    solana_mm = "6JK3LrBvjJaKwCuaJPyg7S4NTHvk2Nx7rBpU4YViBa8S"
    mm_ws._connections.clear()
    mm_ws._connections[solana_mm] = {ws}

    await mm_ws.notify_mm_fill(solana_mm, {"tx_hash": "sig123"})

    assert len(ws.messages) == 1
    mm_ws._connections.clear()
