"""Yield tracking API endpoints."""

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from web3 import Web3

from src.config import settings
from src.contracts.web3_client import get_margin_pool
from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter()

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_ASSET_ADDRESSES = {
    "usdc": settings.usdc_address,
    "eth": settings.weth_address,
    "btc": settings.wbtc_address,
}

_ASSET_DECIMALS = {"usdc": 6, "eth": 18, "btc": 8}


def _human(amount: int | None, asset: str) -> float | None:
    """Convert raw token amount to human-readable float."""
    if amount is None:
        return None
    decimals = _ASSET_DECIMALS.get(asset, 18)
    return amount / (10**decimals)


def _parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _get_accrued_yield() -> dict[str, int | None]:
    """Read current accrued yield from MarginPool for each asset."""
    accrued: dict[str, int | None] = {}
    for asset_symbol, asset_addr in _ASSET_ADDRESSES.items():
        try:
            pool = get_margin_pool()
            checksum = Web3.to_checksum_address(asset_addr)
            accrued[asset_symbol] = pool.functions.getAccruedYield(checksum).call()
        except Exception:
            logger.exception("Failed to read accrued yield for %s", asset_symbol)
            accrued[asset_symbol] = None
    return accrued


def _get_last_distribution_end() -> datetime:
    """Get the end of the last distribution period, or Aave enable date."""
    client = get_client()
    last = (
        client.table("yield_distributions")
        .select("period_end")
        .order("period_end", desc=True)
        .limit(1)
        .execute()
    )
    if last.data:
        return _parse_dt(last.data[0]["period_end"])
    return datetime(2026, 4, 2, 0, 0, 0, tzinfo=timezone.utc)


def _estimate_per_position(
    all_positions: list[dict],
    period_start: datetime,
    period_end: datetime,
    accrued_by_asset: dict[str, int | None],
) -> dict[str, int]:
    """Calculate estimated pending yield per position id.

    Returns {position_id: estimated_raw_amount}.
    """
    fee_bps = settings.protocol_fee_bps

    # Group positions by asset
    by_asset: dict[str, list[dict]] = {}
    for pos in all_positions:
        by_asset.setdefault(pos["asset"], []).append(pos)

    estimates: dict[str, int] = {}

    for asset, positions in by_asset.items():
        total_accrued = accrued_by_asset.get(asset)
        if not total_accrued or total_accrued <= 0:
            continue

        distributable = total_accrued * (10_000 - fee_bps) // 10_000

        # Calculate weights
        weights: list[tuple[str, float]] = []
        total_weight = 0.0
        for pos in positions:
            deposited = _parse_dt(pos["deposited_at"])
            settled_at = _parse_dt(pos["settled_at"]) if pos.get("settled_at") else None
            if settled_at and settled_at <= period_start:
                continue
            start = max(deposited, period_start)
            end = min(settled_at, period_end) if settled_at else period_end
            duration = max((end - start).total_seconds(), 0)
            weight = pos["collateral_amount"] * duration
            weights.append((pos["id"], weight))
            total_weight += weight

        if total_weight == 0:
            continue

        for pos_id, weight in weights:
            share = weight / total_weight
            amount = int(distributable * share)
            if amount > 0:
                estimates[pos_id] = amount

    return estimates


@router.get("/yield/user/{address}", tags=["Yield"], summary="Yield summary per user")
async def get_yield_summary(address: str):
    """Total pending, delivered, and estimated accruing yield per asset."""
    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    addr = address.lower()

    try:
        client = get_client()
        alloc_result = (
            client.table("yield_allocations")
            .select("asset,amount,status")
            .eq("user_address", addr)
            .execute()
        )
        pos_result = (
            client.table("yield_positions")
            .select("id,asset,collateral_amount,deposited_at,settled_at")
            .eq("user_address", addr)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch yield summary for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch yield data")

    alloc_rows = alloc_result.data or []
    pos_rows = pos_result.data or []

    # Aggregate allocations (already distributed or pending distribution)
    by_asset: dict[str, dict] = {}
    for row in alloc_rows:
        asset = row["asset"]
        if asset not in by_asset:
            by_asset[asset] = {"pending": 0, "delivered": 0}
        bucket = "delivered" if row["status"] == "delivered" else "pending"
        by_asset[asset][bucket] += row["amount"]

    # Estimate currently accruing yield (not yet harvested)
    accrued = _get_accrued_yield()
    period_start = _get_last_distribution_end()
    now = datetime.now(tz=timezone.utc)
    estimates = _estimate_per_position(pos_rows, period_start, now, accrued)

    # Sum estimates by asset
    estimated_by_asset: dict[str, int] = {}
    for pos in pos_rows:
        est = estimates.get(pos["id"], 0)
        if est > 0:
            estimated_by_asset[pos["asset"]] = (
                estimated_by_asset.get(pos["asset"], 0) + est
            )

    all_assets = set(by_asset.keys()) | set(estimated_by_asset.keys())
    assets = []
    for asset in sorted(all_assets):
        totals = by_asset.get(asset, {"pending": 0, "delivered": 0})
        est = estimated_by_asset.get(asset, 0)
        assets.append(
            {
                "asset": asset,
                "pending_raw": totals["pending"],
                "pending": _human(totals["pending"], asset),
                "delivered_raw": totals["delivered"],
                "delivered": _human(totals["delivered"], asset),
                "estimated_accruing_raw": est,
                "estimated_accruing": _human(est, asset),
                "total_raw": totals["pending"] + totals["delivered"] + est,
                "total": _human(totals["pending"] + totals["delivered"] + est, asset),
            }
        )

    return {"wallet": addr, "assets": assets}


@router.get(
    "/yield/user/{address}/positions",
    tags=["Yield"],
    summary="Positions with estimated accrued yield",
)
async def get_yield_positions(address: str):
    """List yield-generating positions with per-position estimated yield."""
    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    addr = address.lower()

    try:
        client = get_client()
        result = (
            client.table("yield_positions")
            .select("*")
            .eq("user_address", addr)
            .order("deposited_at", desc=True)
            .limit(500)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch yield positions for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch positions")

    rows = result.data or []

    # Estimate per-position yield from real on-chain accrued
    accrued = _get_accrued_yield()
    period_start = _get_last_distribution_end()
    now = datetime.now(tz=timezone.utc)

    # Need ALL positions (not just this user's) for pro-rata weights
    try:
        all_pos = (
            client.table("yield_positions")
            .select("id,user_address,asset,collateral_amount,deposited_at,settled_at")
            .limit(10000)
            .execute()
        )
        all_positions = all_pos.data or []
    except Exception:
        logger.exception("Failed to fetch all positions for yield estimate")
        all_positions = rows

    estimates = _estimate_per_position(all_positions, period_start, now, accrued)

    positions = []
    total_estimated: dict[str, int] = {}
    for row in rows:
        est = estimates.get(row["id"], 0)
        asset = row["asset"]
        total_estimated[asset] = total_estimated.get(asset, 0) + est
        positions.append(
            {
                "id": row["id"],
                "vault_id": row["vault_id"],
                "asset": asset,
                "collateral_amount": row["collateral_amount"],
                "deposited_at": row["deposited_at"],
                "settled_at": row.get("settled_at"),
                "is_active": row.get("settled_at") is None,
                "estimated_yield_raw": est,
                "estimated_yield": _human(est, asset),
            }
        )

    # Per-asset totals
    totals = [
        {
            "asset": asset,
            "estimated_yield_raw": amount,
            "estimated_yield": _human(amount, asset),
        }
        for asset, amount in sorted(total_estimated.items())
    ]

    return {"wallet": addr, "positions": positions, "totals": totals}


@router.get(
    "/yield/user/{address}/history",
    tags=["Yield"],
    summary="Distribution history with tx hashes",
)
async def get_yield_history(address: str):
    """Past yield distributions with airdrop tx hashes."""
    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    addr = address.lower()

    try:
        client = get_client()
        result = (
            client.table("yield_allocations")
            .select("id,distribution_id,asset,amount,status,airdrop_tx_hash,created_at")
            .eq("user_address", addr)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch yield history for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch history")

    rows = result.data or []
    history = []
    for row in rows:
        history.append(
            {
                "id": row["id"],
                "distribution_id": row["distribution_id"],
                "asset": row["asset"],
                "amount_raw": row["amount"],
                "amount": _human(row["amount"], row["asset"]),
                "status": row["status"],
                "airdrop_tx_hash": row.get("airdrop_tx_hash"),
                "created_at": row["created_at"],
            }
        )

    return {"wallet": addr, "history": history}


@router.get("/yield/stats", tags=["Yield"], summary="Global yield statistics")
async def get_yield_stats():
    """Total yield distributed, fees collected, and estimated APY per asset."""
    try:
        client = get_client()
        result = (
            client.table("yield_distributions")
            .select("asset,total_yield,platform_fee")
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch yield stats")
        raise HTTPException(status_code=502, detail="Could not fetch stats")

    rows = result.data or []

    by_asset: dict[str, dict] = {}
    for row in rows:
        asset = row["asset"]
        if asset not in by_asset:
            by_asset[asset] = {"total_yield": 0, "total_fees": 0, "distributions": 0}
        by_asset[asset]["total_yield"] += row["total_yield"]
        by_asset[asset]["total_fees"] += row["platform_fee"]
        by_asset[asset]["distributions"] += 1

    accrued_by_asset = _get_accrued_yield()

    assets = []
    for asset in _ASSET_ADDRESSES:
        stats = by_asset.get(
            asset, {"total_yield": 0, "total_fees": 0, "distributions": 0}
        )
        accrued_raw = accrued_by_asset.get(asset)
        assets.append(
            {
                "asset": asset,
                "total_yield_raw": stats["total_yield"],
                "total_yield": _human(stats["total_yield"], asset),
                "total_fees_raw": stats["total_fees"],
                "total_fees": _human(stats["total_fees"], asset),
                "total_distributed": _human(
                    stats["total_yield"] - stats["total_fees"], asset
                ),
                "distributions": stats["distributions"],
                "current_accrued_raw": accrued_raw,
                "current_accrued": _human(accrued_raw, asset),
            }
        )

    return {"assets": assets}
