"""Explicit, operator-invoked event backfill capped at one RPC request/second."""

import logging
import time
from datetime import datetime, timedelta, timezone

from src.config import get_tokenized_fund_rpc_url, settings
from src.fund_indexer.collector import (
    MAX_EVENT_BLOCK_RANGE,
    BlockHeader,
    SupabaseCoordinator,
    Web3SnapshotRPC,
)

logger = logging.getLogger(__name__)
BACKFILL_MIN_REQUEST_INTERVAL_SECONDS = 1.0


def run_once(
    coordinator: SupabaseCoordinator,
    rpc: Web3SnapshotRPC,
    environment: str,
    chain_id: int,
    *,
    sleep=time.sleep,
) -> bool:
    try:
        rpc.validate_chain(chain_id)
    except Exception:
        raise RuntimeError("Backfill startup RPC validation failed") from None
    result = coordinator.client.rpc(
        "v2_claim_snapshot_event_backfill",
        {"p_environment": environment, "p_chain_id": chain_id},
    ).execute()
    row = (result.data or [None])[0]
    if not row:
        return False
    start = int(row["from_block"])
    end = int(row["to_block"])
    succeeded = False
    try:
        funds = coordinator.funds(chain_id)
        if row.get("reason") == "checkpoint_reorg_rebuild":
            coordinator.client.rpc(
                "v2_rewind_snapshot_event_backfill",
                {
                    "p_chain_id": chain_id,
                    "p_fund_addresses": [fund.fund_address for fund in funds],
                    "p_rewind_block": start,
                },
            ).execute()
        cursor = start
        last_request_at: float | None = None

        def throttle() -> None:
            nonlocal last_request_at
            if last_request_at is not None:
                delay = BACKFILL_MIN_REQUEST_INTERVAL_SECONDS - (
                    time.monotonic() - last_request_at
                )
                if delay > 0:
                    sleep(delay)
            last_request_at = time.monotonic()

        while cursor <= end:
            chunk_end = min(cursor + MAX_EVENT_BLOCK_RANGE - 1, end)
            rpc.set_deadline(datetime.now(timezone.utc) + timedelta(seconds=30))
            throttle()
            response = rpc.w3.provider.make_request(
                "eth_getBlockByNumber", [hex(chunk_end), False]
            )
            if "error" in response or not response.get("result"):
                raise RuntimeError("Backfill block header RPC failed")
            block = response["result"]
            header = BlockHeader(
                number=chunk_end,
                hash=str(block["hash"]).lower(),
                timestamp=datetime.fromtimestamp(
                    int(block["timestamp"], 16), timezone.utc
                ),
            )
            throttle()
            logs = rpc.event_logs(
                funds, cursor, chunk_end, traffic_class="backfill_rpc"
            )
            ingestions = coordinator.build_ingestions(funds, logs, header)
            coordinator.client.rpc(
                "v2_apply_snapshot_event_backfill_chunk",
                {
                    "p_environment": environment,
                    "p_chain_id": chain_id,
                    "p_from_block": start,
                    "p_to_block": end,
                    "p_ingestions": ingestions,
                    "p_final_chunk": chunk_end == end,
                },
            ).execute()
            for fund in funds:
                fund.inputs["checkpoint"] = {
                    "next_block": chunk_end + 1,
                    "last_block_hash": header.hash,
                }
            cursor = chunk_end + 1
        succeeded = True
        return True
    finally:
        if not succeeded:
            coordinator.client.rpc(
                "v2_finish_snapshot_event_backfill",
                {
                    "p_environment": environment,
                    "p_chain_id": chain_id,
                    "p_from_block": start,
                    "p_to_block": end,
                    "p_succeeded": False,
                },
            ).execute()


def main() -> None:
    rpc_url = get_tokenized_fund_rpc_url()
    if not rpc_url:
        raise RuntimeError("TOKENIZED_FUND_RPC_URL or RPC_URL is required")
    coordinator = SupabaseCoordinator()
    rpc = Web3SnapshotRPC(rpc_url)
    # Explicit command only: no daemon loop and no automatic production activation.
    run_once(coordinator, rpc, settings.app_env, settings.chain_id)


if __name__ == "__main__":
    main()
