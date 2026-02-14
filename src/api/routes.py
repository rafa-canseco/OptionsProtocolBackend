import re

from fastapi import APIRouter, HTTPException

from src.config import settings
from src.db.database import get_client
from src.models.order import AcceptOrderRequest, OrderStatus
from src.models.price import PriceResponse
from src.pricing.chainlink import get_eth_price
from src.pricing.circuit_breaker import circuit_breaker
from src.pricing.deribit import get_eth_iv
from src.pricing.price_sheet import generate_price_sheet

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

router = APIRouter()

# Default available amount per quote (MVP: fixed per strike)
DEFAULT_AVAILABLE_AMOUNT = 10.0  # 10 ETH


@router.get("/prices", response_model=list[PriceResponse])
async def get_prices():
    """Get current price menu for ETH options."""
    if circuit_breaker.is_paused:
        raise HTTPException(
            status_code=503,
            detail=f"Pricing paused: {circuit_breaker.pause_reason}",
        )

    eth_price, _ = get_eth_price()
    iv = await get_eth_iv()

    if circuit_breaker.check(eth_price):
        raise HTTPException(
            status_code=503,
            detail=f"Pricing paused: {circuit_breaker.pause_reason}",
        )

    circuit_breaker.update_reference(eth_price)

    quotes = generate_price_sheet(spot=eth_price, iv=iv)

    return [
        PriceResponse(
            option_type=q.option_type,
            strike=q.strike,
            expiry_days=q.expiry_days,
            premium=q.premium,
            delta=q.delta,
            iv=q.iv,
            spot=q.spot,
            ttl=q.ttl,
            expires_at=q.expires_at,
            available_amount=DEFAULT_AVAILABLE_AMOUNT,
        )
        for q in quotes
    ]


@router.post("/accept")
async def accept_order(req: AcceptOrderRequest):
    """User accepts a quoted price. Stores the order for next batch."""
    if circuit_breaker.is_paused:
        raise HTTPException(status_code=503, detail="Pricing is paused")

    eth_price, _ = get_eth_price()
    iv = await get_eth_iv()

    spot_drift = abs(eth_price - req.spot_at_lock) / req.spot_at_lock
    if spot_drift > 0.005:  # 0.5% tolerance
        raise HTTPException(
            status_code=400,
            detail=f"Price moved {spot_drift:.2%} since quote. Please refresh.",
        )

    client = get_client()
    result = (
        client.table("orders")
        .insert(
            {
                "user_address": req.user_address,
                "option_type": req.option_type,
                "strike": req.strike,
                "expiry_days": req.expiry_days,
                "premium": req.premium,
                "spot_at_lock": req.spot_at_lock,
                "iv_at_lock": req.iv_at_lock,
                "status": OrderStatus.PENDING.value,
            }
        )
        .execute()
    )

    order = result.data[0]
    return {"order_id": order["id"], "status": "pending"}


@router.get("/positions/{address}")
async def get_positions(address: str):
    """Get all orders for a user address."""
    if not ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")

    client = get_client()
    result = (
        client.table("orders")
        .select("*")
        .eq("user_address", address)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


@router.get("/batch/status")
async def batch_status():
    """Get next batch countdown and pending orders count."""
    client = get_client()

    pending = (
        client.table("orders")
        .select("id", count="exact")
        .eq("status", OrderStatus.PENDING.value)
        .execute()
    )

    latest_batch = (
        client.table("batches")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    last_batch_time = None
    if latest_batch.data:
        last_batch_time = latest_batch.data[0]["created_at"]

    return {
        "pending_orders": pending.count or 0,
        "batch_interval_minutes": settings.batch_interval_minutes,
        "last_batch_at": last_batch_time,
        "circuit_breaker": circuit_breaker.status,
    }
