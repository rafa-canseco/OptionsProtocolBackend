"""
Weekly Yield Airdrop Service

Harvests Aave yield from MarginPool, calculates per-position allocations,
and distributes tokens to users. Run every Monday via cron.

Usage:
    uv run python -m src.bots.runner yield_airdrop
"""

import asyncio
import logging
from datetime import datetime, timezone

from web3 import Web3

from src.config import settings
from src.contracts.web3_client import (
    build_and_send_tx,
    get_erc20,
    get_margin_pool,
    get_operator_account,
    get_w3,
)
from src.db.database import get_client
from src.yield_tracking.calculator import calculate_allocations, save_allocations

logger = logging.getLogger(__name__)

# Aave enabled on 2026-04-02 — first period starts here
_AAVE_ENABLE = datetime(2026, 4, 2, 0, 0, 0, tzinfo=timezone.utc)

_ASSET_ADDRESSES = {
    "usdc": settings.usdc_address,
    "eth": settings.weth_address,
    "btc": settings.wbtc_address,
}


def _harvest(asset_symbol: str) -> tuple[str, int] | None:
    """Call harvestYield(asset) on MarginPool.

    Returns (tx_hash, actual_harvested_amount) from the transaction
    receipt, not the pre-call estimate.
    """
    asset_addr = _ASSET_ADDRESSES.get(asset_symbol)
    if not asset_addr:
        logger.error("Unknown asset symbol: %s", asset_symbol)
        return None

    pool = get_margin_pool()
    checksum_addr = Web3.to_checksum_address(asset_addr)

    accrued = pool.functions.getAccruedYield(checksum_addr).call()
    if accrued == 0:
        logger.info("No accrued yield for %s, skipping harvest", asset_symbol)
        return None

    logger.info("Harvesting %s yield (estimated: %d)", asset_symbol, accrued)
    account = get_operator_account()
    tx_hash = build_and_send_tx(
        pool.functions.harvestYield(checksum_addr),
        account,
    )

    # Read actual harvested amount from receipt events
    w3 = get_w3()
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    actual = accrued  # fallback to estimate if event parsing fails
    for log_entry in receipt.get("logs", []):
        try:
            decoded = pool.events.YieldHarvested().process_log(log_entry)
            actual = getattr(decoded.args, "yield", accrued)
            break
        except Exception:
            continue

    logger.info(
        "Harvested %s yield: tx=%s actual=%d (estimated=%d)",
        asset_symbol,
        tx_hash,
        actual,
        accrued,
    )
    return tx_hash, actual


def _transfer_fee_to_treasury(asset_symbol: str, amount: int) -> str | None:
    """Transfer protocol fee to treasury. Returns tx hash."""
    if amount == 0:
        return None
    asset_addr = _ASSET_ADDRESSES.get(asset_symbol)
    if not asset_addr:
        return None

    token = get_erc20(asset_addr)
    treasury = Web3.to_checksum_address(settings.treasury_address)
    account = get_operator_account()

    tx_hash = build_and_send_tx(
        token.functions.transfer(treasury, amount),
        account,
    )
    logger.info(
        "Transferred %d %s fee to treasury: tx=%s",
        amount,
        asset_symbol,
        tx_hash,
    )
    return tx_hash


def _distribute_allocations(allocations: list[dict]) -> tuple[int, int]:
    """Execute ERC20 transfers for each allocation.

    Returns (delivered_count, failed_count).
    """
    account = get_operator_account()
    client = get_client()
    delivered = 0
    failed = 0

    for alloc in allocations:
        asset_addr = _ASSET_ADDRESSES.get(alloc["asset"])
        if not asset_addr:
            logger.error("Unknown asset in allocation: %s", alloc["asset"])
            failed += 1
            continue

        token = get_erc20(asset_addr)
        recipient = Web3.to_checksum_address(alloc["user_address"])

        try:
            tx_hash = build_and_send_tx(
                token.functions.transfer(recipient, alloc["amount"]),
                account,
            )
        except Exception:
            logger.exception(
                "Failed to transfer %d %s to %s (alloc_id=%s)",
                alloc["amount"],
                alloc["asset"],
                alloc["user_address"][:10],
                alloc.get("id"),
            )
            failed += 1
            continue

        client.table("yield_allocations").update(
            {
                "status": "delivered",
                "airdrop_tx_hash": tx_hash,
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            }
        ).eq("id", alloc["id"]).execute()

        delivered += 1
        logger.info(
            "Delivered %d %s to %s: tx=%s",
            alloc["amount"],
            alloc["asset"],
            alloc["user_address"][:10],
            tx_hash[:16],
        )

    return delivered, failed


def run_airdrop(period_start: datetime, period_end: datetime) -> dict:
    """Execute the full harvest → calculate → distribute cycle.

    Returns a summary dict with counts per asset.
    Each asset is processed independently — one failure does not block others.
    """
    summary: dict[str, dict] = {}

    for asset_symbol in _ASSET_ADDRESSES:
        try:
            result = _process_asset(asset_symbol, period_start, period_end)
            summary[asset_symbol] = result
        except Exception:
            logger.exception("Yield airdrop failed for asset %s", asset_symbol)
            summary[asset_symbol] = {"error": True, "harvested": 0}

    return summary


def _process_asset(
    asset_symbol: str,
    period_start: datetime,
    period_end: datetime,
) -> dict:
    """Process harvest + allocation + distribution for a single asset."""
    harvest_result = _harvest(asset_symbol)
    if harvest_result is None:
        return {"harvested": 0, "allocations": 0, "delivered": 0, "failed": 0}

    tx_hash, total_yield = harvest_result

    # Fee computed once here — single source of truth
    fee_bps = settings.protocol_fee_bps
    platform_fee = total_yield * fee_bps // 10_000
    distributable = total_yield - platform_fee

    # Create distribution record
    client = get_client()
    dist_result = (
        client.table("yield_distributions")
        .insert(
            {
                "harvest_tx_hash": tx_hash,
                "asset": asset_symbol,
                "total_yield": total_yield,
                "platform_fee": platform_fee,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "distributed_at": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
        .execute()
    )
    distribution_id = dist_result.data[0]["id"]

    # Transfer fee to treasury
    try:
        _transfer_fee_to_treasury(asset_symbol, platform_fee)
    except Exception:
        logger.exception(
            "Fee transfer to treasury failed for %s (fee=%d). "
            "Continuing with user distributions.",
            asset_symbol,
            platform_fee,
        )

    # Calculate and save allocations (pass distributable, not total_yield)
    allocations, dust = calculate_allocations(
        distribution_id, period_start, period_end, asset_symbol, distributable
    )
    save_allocations(allocations)

    # Distribute to users
    saved = (
        client.table("yield_allocations")
        .select("id,user_address,asset,amount")
        .eq("distribution_id", distribution_id)
        .eq("status", "pending")
        .execute()
    )
    delivered, failed = _distribute_allocations(saved.data or [])

    result = {
        "harvested": total_yield,
        "platform_fee": platform_fee,
        "distributable": distributable,
        "allocations": len(allocations),
        "delivered": delivered,
        "failed": failed,
        "dust": dust,
    }
    logger.info("Asset %s: %s", asset_symbol, result)

    if failed > 0:
        logger.error(
            "%d/%d transfers failed for %s — pending allocations remain",
            failed,
            len(allocations),
            asset_symbol,
        )

    return result


async def run() -> None:
    """Entry point for runner.py. Determines period and runs airdrop once."""
    now = datetime.now(tz=timezone.utc)

    # Check for last distribution to determine period_start
    client = get_client()
    last = (
        client.table("yield_distributions")
        .select("period_end")
        .order("period_end", desc=True)
        .limit(1)
        .execute()
    )

    if last.data:
        period_start = datetime.fromisoformat(
            last.data[0]["period_end"].replace("Z", "+00:00")
        )
    else:
        period_start = _AAVE_ENABLE

    period_end = now

    logger.info(
        "Running yield airdrop for period %s → %s",
        period_start.isoformat(),
        period_end.isoformat(),
    )

    # Run blocking airdrop in a thread to avoid freezing the event loop
    summary = await asyncio.to_thread(run_airdrop, period_start, period_end)
    logger.info("Airdrop complete: %s", summary)
