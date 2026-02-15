import re

from fastapi import APIRouter, HTTPException

from src.db.database import get_client
from src.models.price import PriceResponse
from src.pricing.chainlink import get_eth_price
from src.pricing.circuit_breaker import circuit_breaker
from src.pricing.deribit import get_eth_iv
from src.pricing.price_sheet import generate_price_sheet

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

router = APIRouter()

DEFAULT_AVAILABLE_AMOUNT = 10.0


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


@router.get("/positions/{address}")
async def get_positions(address: str):
    """Get all positions for a user address (from indexed on-chain events)."""
    if not ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")

    client = get_client()
    result = (
        client.table("order_events")
        .select("*")
        .eq("user_address", address.lower())
        .order("indexed_at", desc=True)
        .execute()
    )
    return result.data
