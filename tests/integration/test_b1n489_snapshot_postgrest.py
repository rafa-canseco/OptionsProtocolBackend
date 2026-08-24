import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

URL = os.getenv("B1N489_POSTGREST_URL", "")
SERVICE = os.getenv("B1N489_SERVICE_TOKEN", "")
ANON = os.getenv("B1N489_ANON_TOKEN", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="B1N-489 PostgREST fixture is unavailable"),
]


def post(function: str, body: dict, token: str = SERVICE) -> httpx.Response:
    return httpx.post(
        f"{URL}/rpc/{function}",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=10,
    )


def fixture() -> dict:
    return json.loads(Path("tests/fixtures/rpc_snapshot_envelope.json").read_text())


def claim_one(environment: str) -> dict:
    for _attempt in range(15):
        response = post(
            "v2_claim_snapshot_window",
            {"p_environment": environment, "p_chain_id": 84532},
        )
        assert response.status_code == 200, response.text
        if response.json():
            return response.json()[0]
        time.sleep(1)
    raise AssertionError("claim window never opened")


def publish_payload(environment: str, claim: dict, raw: dict) -> httpx.Response:
    return post(
        "v2_publish_snapshot",
        {
            "p_environment": environment,
            "p_chain_id": 84532,
            "p_window_id": claim["window_id"],
            "p_claim_token": claim["claim_token"],
            "p_snapshot_block": 1000,
            "p_snapshot_block_hash": raw["snapshot_block_hash"],
            "p_snapshot_block_timestamp": datetime.now(timezone.utc).isoformat(),
            "p_common": raw["common"],
            "p_funds": raw["funds"],
        },
    )


def service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE}", "Prefer": "return=representation"}


def test_reorg_rewind_removes_orphaned_history_before_rebuild() -> None:
    fund = "0xcccccccccccccccccccccccccccccccccccccccc"
    event = {
        "chain_id": 84532,
        "fund_address": fund,
        "contract_address": fund,
        "contract_role": "fund_vault",
        "interface_version": 1,
        "block_number": 1,
        "block_hash": "0x" + "11" * 32,
        "transaction_hash": "0x" + "22" * 32,
        "transaction_index": 0,
        "log_index": 0,
        "event_name": "NavInvalidated",
        "payload": {"positionsHash": "0x" + "33" * 32},
    }
    assert (
        httpx.post(
            f"{URL}/v2_chain_events", headers=service_headers(), json=event
        ).status_code
        == 201
    )
    checkpoint = {
        "chain_id": 84532,
        "fund_address": fund,
        "indexer_name": "tokenized_csp_fund",
        "next_block": 2,
        "last_block_hash": event["block_hash"],
    }
    assert (
        httpx.post(
            f"{URL}/v2_indexer_checkpoints",
            headers=service_headers(),
            json=checkpoint,
        ).status_code
        == 201
    )
    rewound = post(
        "v2_rewind_snapshot_event_backfill",
        {
            "p_chain_id": 84532,
            "p_fund_addresses": [fund],
            "p_rewind_block": 1,
        },
    )
    assert rewound.status_code == 200 and rewound.json() is True
    events = httpx.get(
        f"{URL}/v2_chain_events",
        headers=service_headers(),
        params={"chain_id": "eq.84532", "fund_address": f"eq.{fund}"},
    )
    assert events.status_code == 200 and events.json() == []
    checkpoint_rows = httpx.get(
        f"{URL}/v2_indexer_checkpoints",
        headers=service_headers(),
        params={
            "chain_id": "eq.84532",
            "fund_address": f"eq.{fund}",
            "indexer_name": "eq.tokenized_csp_fund",
        },
    ).json()
    assert checkpoint_rows[0]["next_block"] == 1
    assert checkpoint_rows[0]["last_block_hash"] is None
    restored = httpx.post(
        f"{URL}/v2_fund_state",
        headers=service_headers(),
        json={"chain_id": 84532, "fund_address": fund, "last_event_block": 1},
    )
    assert restored.status_code == 201, restored.text


def test_backfill_chunk_rolls_back_all_funds_when_one_fails() -> None:
    work = {
        "environment": "integration",
        "chain_id": 84532,
        "from_block": 1,
        "to_block": 1,
        "reason": "integration",
    }
    created = httpx.post(
        f"{URL}/v2_snapshot_event_backfills",
        headers=service_headers(),
        json=work,
    )
    assert created.status_code == 201, created.text
    claimed = post(
        "v2_claim_snapshot_event_backfill",
        {"p_environment": "integration", "p_chain_id": 84532},
    )
    assert claimed.status_code == 200 and claimed.json()
    ingestions = [
        {
            "chain_id": 84532,
            "fund_address": "0x4444444444444444444444444444444444444444",
            "indexer_name": "tokenized_csp_fund",
            "from_block": 1,
            "to_block": 1,
            "last_block_hash": "0x" + "aa" * 32,
            "events": [],
            "projection": {},
        },
        {
            "chain_id": 84532,
            "fund_address": "0x0000000000000000000000000000000000000001",
            "indexer_name": "tokenized_csp_fund",
            "from_block": 1,
            "to_block": 1,
            "last_block_hash": "0x" + "aa" * 32,
            "events": [],
            "projection": {},
        },
    ]
    failed = post(
        "v2_apply_snapshot_event_backfill_chunk",
        {
            "p_environment": "integration",
            "p_chain_id": 84532,
            "p_from_block": 1,
            "p_to_block": 1,
            "p_ingestions": ingestions,
            "p_final_chunk": True,
        },
    )
    assert failed.status_code == 400
    checkpoints = httpx.get(
        f"{URL}/v2_indexer_checkpoints",
        headers=service_headers(),
        params={
            "chain_id": "eq.84532",
            "fund_address": "eq.0x4444444444444444444444444444444444444444",
            "indexer_name": "eq.tokenized_csp_fund",
        },
    )
    assert checkpoints.status_code == 200 and checkpoints.json() == []


def test_claim_publish_get_and_malformed_atomic_rejection() -> None:
    claim_body = {"p_environment": "integration", "p_chain_id": 84532}
    winners = []
    for _attempt in range(15):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: post("v2_claim_snapshot_window", claim_body), range(2)
                )
            )
        assert all(response.status_code == 200 for response in responses), [
            (response.status_code, response.text) for response in responses
        ]
        winners = [
            rows[0] for rows in (response.json() for response in responses) if rows
        ]
        if winners:
            break
        time.sleep(1)
    assert len(winners) == 1
    winner = winners[0]

    raw = fixture()
    publish = publish_payload("integration", winner, raw)
    assert publish.status_code == 200, publish.text
    generation = int(publish.json())

    current = post(
        "v2_get_current_snapshot",
        {"p_environment": "integration", "p_chain_id": 84532},
    )
    assert current.status_code == 200
    envelope = current.json()
    assert envelope["generation"] == generation
    assert envelope["common"] == raw["common"]
    assert envelope["funds"] == raw["funds"]
    assert envelope["reconciled"] is True
    assert envelope["stale"] is False

    rejected = post("v2_claim_snapshot_window", claim_body, token=ANON)
    assert rejected.status_code in {401, 403, 404}

    malformed = fixture()
    next(iter(malformed["funds"][0]["state"]["allocator"]["series"].values())).pop(
        "expiry"
    )
    assert (
        publish_payload("malformed", claim_one("malformed"), malformed).status_code
        == 400
    )
    absent = post(
        "v2_get_current_snapshot",
        {"p_environment": "malformed", "p_chain_id": 84532},
    )
    assert absent.status_code == 200 and absent.json() is None

    malformed_common = fixture()
    malformed_common["common"]["quotes"][0]["unexpected"] = True
    assert (
        publish_payload(
            "bad-common", claim_one("bad-common"), malformed_common
        ).status_code
        == 400
    )

    malformed_call = fixture()
    malformed_call["funds"][1]["state"]["allocator"]["valuation_observers"] = [True]
    assert (
        publish_payload(
            "bad-covered", claim_one("bad-covered"), malformed_call
        ).status_code
        == 400
    )

    malformed_meta = fixture()
    malformed_meta["funds"][2]["state"]["allocator"]["wheel_quotes"][0].pop("lane")
    assert (
        publish_payload("bad-meta", claim_one("bad-meta"), malformed_meta).status_code
        == 400
    )
