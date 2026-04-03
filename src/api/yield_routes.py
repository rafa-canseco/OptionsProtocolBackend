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

_AAVE_ENABLE = datetime(2026, 4, 2, 0, 0, 0, tzinfo=timezone.utc)

_ASSET_ADDRESSES = {
    "usdc": settings.usdc_address,
    "eth": settings.weth_address,
    "btc": settings.wbtc_address,
}

_ASSET_DECIMALS = {"usdc": 6, "eth": 18, "btc": 8}


def _human(amount: int, asset: str) -> float:
    """Convert raw token amount to human-readable float."""
    decimals = _ASSET_DECIMALS.get(asset, 18)
    return amount / (10**decimals)


@router.get("/yield/user/{address}", tags=["Yield"], summary="Yield summary per user")
async def get_yield_summary(address: str):
    """Total pending and delivered yield for a wallet, broken down by asset."""
    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    addr = address.lower()

    try:
        client = get_client()
        result = (
            client.table("yield_allocations")
            .select("asset,amount,status")
            .eq("user_address", addr)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch yield summary for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch yield data")

    rows = result.data or []

    by_asset: dict[str, dict] = {}
    for row in rows:
        asset = row["asset"]
        if asset not in by_asset:
            by_asset[asset] = {"pending": 0, "delivered": 0}
        bucket = "delivered" if row["status"] == "delivered" else "pending"
        by_asset[asset][bucket] += row["amount"]

    assets = []
    for asset, totals in by_asset.items():
        assets.append(
            {
                "asset": asset,
                "pending_raw": totals["pending"],
                "pending": _human(totals["pending"], asset),
                "delivered_raw": totals["delivered"],
                "delivered": _human(totals["delivered"], asset),
                "total_raw": totals["pending"] + totals["delivered"],
                "total": _human(totals["pending"] + totals["delivered"], asset),
            }
        )

    return {"wallet": addr, "assets": assets}


@router.get(
    "/yield/user/{address}/positions",
    tags=["Yield"],
    summary="Positions with accrued yield",
)
async def get_yield_positions(address: str):
    """List yield-generating positions with estimated accrued yield."""
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

    positions = []
    for row in rows:
        positions.append(
            {
                "id": row["id"],
                "vault_id": row["vault_id"],
                "asset": row["asset"],
                "collateral_amount": row["collateral_amount"],
                "deposited_at": row["deposited_at"],
                "settled_at": row.get("settled_at"),
                "is_active": row.get("settled_at") is None,
            }
        )

    return {"wallet": addr, "positions": positions}


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

    # Read current accrued yield from MarginPool (if available)
    accrued_by_asset: dict[str, int] = {}
    for asset_symbol, asset_addr in _ASSET_ADDRESSES.items():
        try:
            pool = get_margin_pool()
            checksum = Web3.to_checksum_address(asset_addr)
            accrued = pool.functions.getAccruedYield(checksum).call()
            accrued_by_asset[asset_symbol] = accrued
        except Exception:
            accrued_by_asset[asset_symbol] = 0

    assets = []
    for asset in _ASSET_ADDRESSES:
        stats = by_asset.get(
            asset, {"total_yield": 0, "total_fees": 0, "distributions": 0}
        )
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
                "current_accrued_raw": accrued_by_asset.get(asset, 0),
                "current_accrued": _human(accrued_by_asset.get(asset, 0), asset),
            }
        )

    return {"assets": assets}
