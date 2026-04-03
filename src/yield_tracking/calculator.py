"""
Yield Calculator

Time-weighted pro-rata allocation of harvested Aave yield to individual
positions. Each asset (USDC, WETH, cbBTC) is calculated independently.
"""

import logging
from datetime import datetime

from src.config import settings
from src.db.database import get_client

logger = logging.getLogger(__name__)


def _parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def calculate_allocations(
    distribution_id: str,
    period_start: datetime,
    period_end: datetime,
    asset: str,
    distributable: int,
) -> tuple[list[dict], int]:
    """Calculate time-weighted pro-rata yield allocations for a period.

    Args:
        distributable: Post-fee amount to distribute to users.

    Returns:
        (allocations, dust) where dust is the rounding remainder.
    """
    client = get_client()
    result = (
        client.table("yield_positions")
        .select("id,user_address,collateral_amount,deposited_at,settled_at")
        .eq("asset", asset)
        .lt("deposited_at", period_end.isoformat())
        .execute()
    )

    if result.data is None:
        logger.error("yield_positions query returned None for asset=%s", asset)
        return [], 0

    # Filter: positions active during the period
    positions = []
    for row in result.data:
        settled = row.get("settled_at")
        if settled and _parse_dt(settled) <= period_start:
            continue
        positions.append(row)

    if not positions:
        logger.warning("No active positions for asset=%s in period", asset)
        return [], 0

    # Calculate time-weighted shares
    weights: list[tuple[dict, float]] = []
    total_weight = 0.0

    for pos in positions:
        deposited = _parse_dt(pos["deposited_at"])
        settled_at = _parse_dt(pos["settled_at"]) if pos.get("settled_at") else None

        start = max(deposited, period_start)
        end = min(settled_at, period_end) if settled_at else period_end
        duration = max((end - start).total_seconds(), 0)

        weight = pos["collateral_amount"] * duration
        weights.append((pos, weight))
        total_weight += weight

    if total_weight == 0:
        logger.warning("Total weight is zero for asset=%s", asset)
        return [], 0

    allocations = []
    allocated_total = 0
    for pos, weight in weights:
        share = weight / total_weight
        amount = int(distributable * share)
        if amount == 0:
            continue
        allocated_total += amount
        allocations.append(
            {
                "distribution_id": distribution_id,
                "position_id": pos["id"],
                "user_address": pos["user_address"],
                "asset": asset,
                "amount": amount,
                "status": "pending",
            }
        )

    # Assign rounding dust to largest allocation
    dust = distributable - allocated_total
    if dust > 0 and allocations:
        allocations[0]["amount"] += dust
        allocated_total += dust

    if dust > 0:
        logger.info("Assigned %d dust to largest allocation for %s", dust, asset)

    return allocations, dust


def save_allocations(allocations: list[dict]) -> int:
    """Insert allocation rows into yield_allocations. Returns count."""
    if not allocations:
        return 0
    client = get_client()
    client.table("yield_allocations").insert(allocations).execute()
    logger.info("Saved %d yield allocations", len(allocations))
    return len(allocations)


def estimate_pending_yield(
    user_address: str,
    asset: str,
    period_start: datetime,
    period_end: datetime,
    total_accrued: int,
) -> int:
    """Estimate a user's share of currently accrued (unharvested) yield.

    Returns the estimated yield amount as a raw integer (post-fee).
    """
    client = get_client()
    result = (
        client.table("yield_positions")
        .select("id,user_address,collateral_amount,deposited_at,settled_at")
        .eq("asset", asset)
        .lt("deposited_at", period_end.isoformat())
        .execute()
    )

    if not result.data:
        return 0

    positions = []
    for row in result.data:
        settled = row.get("settled_at")
        if settled and _parse_dt(settled) <= period_start:
            continue
        positions.append(row)

    user_weight = 0.0
    total_weight = 0.0

    for pos in positions:
        deposited = _parse_dt(pos["deposited_at"])
        settled_at = _parse_dt(pos["settled_at"]) if pos.get("settled_at") else None

        start = max(deposited, period_start)
        end = min(settled_at, period_end) if settled_at else period_end
        duration = max((end - start).total_seconds(), 0)

        weight = pos["collateral_amount"] * duration
        total_weight += weight
        if pos["user_address"].lower() == user_address.lower():
            user_weight += weight

    if total_weight == 0:
        return 0

    fee_bps = settings.protocol_fee_bps
    distributable = total_accrued * (10_000 - fee_bps) // 10_000
    return int(distributable * user_weight / total_weight)
