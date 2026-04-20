#!/usr/bin/env python3
"""Backfill collateral_usd for existing order_events rows.

Usage:
    uv run scripts/backfill_collateral_usd.py           # dry run
    uv run scripts/backfill_collateral_usd.py --dry-run  # explicit dry run
    uv run scripts/backfill_collateral_usd.py --apply    # write to DB

Fetches all order_events where collateral_usd IS NULL,
computes the USD value using historical Chainlink prices at each position's
block_number, then updates the DB row.

PUT options use USDC collateral (6 decimals), so no Chainlink call is needed.
CALL ETH uses 1e18 denominator; CALL BTC uses 1e8.
"""

import logging
import sys

from src.db.database import get_client
from src.pricing.assets import Asset
from src.pricing.chainlink import get_asset_price_at_block
from src.pricing.utils import collateral_to_usd

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_ASSET_ENUM: dict[str, Asset] = {
    "eth": Asset.ETH,
    "btc": Asset.BTC,
}


def _compute_collateral_usd(
    row: dict,
    price_cache: dict[tuple[str, int], float],
) -> float:
    """Return collateral_usd for a single row, fetching Chainlink price if needed."""
    is_put = row.get("is_put")
    asset_str = (row.get("asset") or "eth").lower()

    # PUT options are collateralized in USDC — no spot price needed
    if is_put is True or is_put is None:
        return collateral_to_usd(row, 0.0, 0.0)

    # CALL option — need historical spot price at the block this position was created
    block_number = row.get("block_number")
    if block_number is None:
        raise ValueError(
            f"Row id={row['id']} has NULL block_number — cannot fetch historical price"
        )

    cache_key = (asset_str, block_number)
    if cache_key not in price_cache:
        asset_enum = _ASSET_ENUM.get(asset_str)
        if asset_enum is None:
            raise ValueError(f"Unknown asset '{asset_str}' for row id={row['id']}")
        price_cache[cache_key] = get_asset_price_at_block(asset_enum, block_number)

    spot = price_cache[cache_key]
    if asset_str == "btc":
        return collateral_to_usd(row, 0.0, spot)
    return collateral_to_usd(row, spot, 0.0)


def backfill(apply: bool = False) -> None:
    client = get_client()

    result = (
        client.table("order_events")
        .select("id,collateral,is_put,asset,block_number")
        .is_("collateral_usd", "null")
        .execute()
    )
    if result.data is None:
        logger.error("Query returned None — possible auth or network error")
        sys.exit(1)

    rows = result.data
    logger.info("Found %d rows with collateral_usd = NULL", len(rows))

    price_cache: dict[tuple[str, int], float] = {}
    updated = 0
    failed = 0

    for row in rows:
        row_id = row["id"]
        try:
            value = _compute_collateral_usd(row, price_cache)
        except Exception:
            logger.exception("Failed to compute collateral_usd for row id=%s", row_id)
            failed += 1
            continue

        if apply:
            try:
                client.table("order_events").update({"collateral_usd": value}).eq(
                    "id", row_id
                ).execute()
                logger.info("Updated row %s: collateral_usd = %.2f", row_id, value)
            except Exception:
                logger.exception("Failed to update row id=%s", row_id)
                failed += 1
                continue
        else:
            logger.info("Would update row %s: collateral_usd = %.2f", row_id, value)

        updated += 1

    action = "Updated" if apply else "Would update"
    logger.info("Done. %s %d rows.", action, updated)
    if not apply and updated > 0:
        logger.info("Dry run. Re-run with --apply to write to DB.")

    if failed > 0:
        logger.error("%d rows could not be backfilled. See errors above.", failed)
        sys.exit(1)


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    backfill(apply=apply)
