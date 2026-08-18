"""Replay Base events to measure eager vs. first-use oToken creation.

Example:
  uv run python scripts/replay_lazy_otoken_creations.py \
    --from-block 27600000 --to-block 30500000
"""

import argparse
import json

from src.config import settings
from src.contracts.web3_client import (
    get_batch_settler,
    get_otoken_factory,
    get_w3,
)


def summarize(created: set[str], traded: set[str]) -> dict:
    """Return a conservative lazy-creation counter.

    Every distinct traded address is counted as a hypothetical creation even
    when it pre-dated the observed factory window. This intentionally
    understates the savings.
    """
    eager = len(created)
    lazy_upper_bound = len(traded)
    reduction = 0.0 if eager == 0 else 1 - (lazy_upper_bound / eager)
    return {
        "eager_created": eager,
        "unique_traded": lazy_upper_bound,
        "created_and_traded": len(created & traded),
        "traded_preexisting_or_other_factory": len(traded - created),
        "unused_created": len(created - traded),
        "creation_reduction_fraction": reduction,
        "creation_reduction_percent": round(reduction * 100, 4),
        "passes_90_percent_target": reduction >= 0.90,
    }


def fetch_addresses(from_block: int, to_block: int, chunk_size: int) -> tuple[set, set]:
    factory = get_otoken_factory()
    settler = get_batch_settler()
    created: set[str] = set()
    traded: set[str] = set()
    cursor = from_block
    while cursor <= to_block:
        end = min(to_block, cursor + chunk_size - 1)
        for event in factory.events.OTokenCreated.get_logs(
            from_block=cursor, to_block=end
        ):
            created.add(event.args.oToken.lower())
        for event in settler.events.OrderExecuted.get_logs(
            from_block=cursor, to_block=end
        ):
            traded.add(event.args.oToken.lower())
        cursor = end + 1
    return created, traded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-block", required=True, type=int)
    parser.add_argument("--to-block", type=int)
    parser.add_argument("--chunk-size", default=2_000, type=int)
    args = parser.parse_args()
    to_block = args.to_block
    if to_block is None:
        to_block = get_w3().eth.block_number
    created, traded = fetch_addresses(
        args.from_block,
        to_block,
        args.chunk_size,
    )
    print(
        json.dumps(
            {
                "chain_id": settings.chain_id,
                "factory": settings.otoken_factory_address,
                "batch_settler": settings.batch_settler_address,
                "from_block": args.from_block,
                "to_block": to_block,
                **summarize(created, traded),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
