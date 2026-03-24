"""
One-time backfill: assign group_id to existing range position pairs.

Heuristic for matching:
  - Same user_address
  - Same expiry
  - Same asset
  - One is_put=True, one is_put=False
  - indexed_at within 60 seconds of each other

Ambiguous matches (3+ candidates) are logged and skipped.

Usage:
    uv run python scripts/backfill_group_id.py          # dry run
    uv run python scripts/backfill_group_id.py --apply   # write to DB
"""

import logging
import sys
import uuid
from datetime import datetime, timezone

from src.db.database import get_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


def _parse_ts(ts_str: str) -> float:
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return (
        dt.replace(tzinfo=timezone.utc).timestamp()
        if dt.tzinfo is None
        else dt.timestamp()
    )


def backfill(apply: bool = False) -> None:
    client = get_client()

    result = (
        client.table("order_events")
        .select("id,tx_hash,user_address,expiry,is_put,asset,indexed_at,group_id")
        .is_("group_id", "null")
        .not_.is_("expiry", "null")
        .not_.is_("is_put", "null")
        .order("indexed_at")
        .execute()
    )
    if result.data is None:
        logger.error("Query returned None — possible auth or network error")
        sys.exit(1)
    rows = result.data
    logger.info("Found %d ungrouped positions", len(rows))

    # Group candidates by (user, expiry, asset)
    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["user_address"], r["expiry"], r.get("asset") or "eth")
        buckets.setdefault(key, []).append(r)

    paired = 0
    skipped_ambiguous = 0

    for key, candidates in buckets.items():
        puts = [c for c in candidates if c["is_put"] is True]
        calls = [c for c in candidates if c["is_put"] is False]

        if not puts or not calls:
            continue

        # Try to pair each put with the closest call within window
        used_call_ids: set[str] = set()
        for put in puts:
            put_ts = _parse_ts(put["indexed_at"])
            best_call = None
            best_delta = WINDOW_SECONDS + 1

            for call in calls:
                if call["id"] in used_call_ids:
                    continue
                call_ts = _parse_ts(call["indexed_at"])
                delta = abs(put_ts - call_ts)
                if delta <= WINDOW_SECONDS and delta < best_delta:
                    best_call = call
                    best_delta = delta

            if best_call is None:
                continue

            # Check for ambiguity: multiple calls within window
            close_calls = [
                c
                for c in calls
                if c["id"] not in used_call_ids
                and abs(_parse_ts(c["indexed_at"]) - put_ts) <= WINDOW_SECONDS
            ]
            if len(close_calls) > 1:
                logger.warning(
                    "Ambiguous: user=%s expiry=%s has %d calls within %ds of put %s. Skipping.",
                    key[0],
                    key[1],
                    len(close_calls),
                    WINDOW_SECONDS,
                    put["tx_hash"],
                )
                skipped_ambiguous += 1
                continue

            gid = str(uuid.uuid4())
            logger.info(
                "Pair: put=%s call=%s delta=%.0fs -> group_id=%s",
                put["tx_hash"][:10],
                best_call["tx_hash"][:10],
                best_delta,
                gid[:8],
            )

            if apply:
                try:
                    client.table("order_events").update({"group_id": gid}).in_(
                        "id", [put["id"], best_call["id"]]
                    ).execute()
                except Exception:
                    logger.exception(
                        "Failed to write group_id for put=%s call=%s",
                        put["tx_hash"][:10],
                        best_call["tx_hash"][:10],
                    )
                    continue

            used_call_ids.add(best_call["id"])
            paired += 1

    logger.info(
        "Paired %d range groups, skipped %d ambiguous", paired, skipped_ambiguous
    )
    if not apply and paired > 0:
        logger.info("Dry run. Re-run with --apply to write to DB.")


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv)
