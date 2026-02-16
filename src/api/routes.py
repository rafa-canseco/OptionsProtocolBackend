import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException

from src.db.database import get_client
from src.models.price import PriceResponse
from src.pricing.chainlink import get_eth_price
from src.pricing.circuit_breaker import circuit_breaker
from src.pricing.deribit import get_eth_iv
from src.pricing.price_sheet import PriceQuote, generate_price_sheet

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_AVAILABLE_AMOUNT = 10.0
MAX_EXPIRY_DIFF_DAYS = 3


def _quote_key(q: PriceQuote) -> tuple:
    """Key to match a BS quote to an oToken: (type, strike, expiry_days)."""
    return (q.option_type.value, q.strike, q.expiry_days)


def _build_otoken_map(quotes: list[PriceQuote]) -> dict[tuple, str]:
    """Map (type, strike, expiry_days) → oToken address using on-chain data.

    Reuses the same matching logic as price_publisher.match_quotes_to_otokens:
    match by option type, strike within $1, closest expiry (max 3 days diff).
    Returns empty dict on failure — otoken_address will be null for all quotes.
    """
    try:
        from src.bots.price_publisher import discover_active_otokens
        otokens = discover_active_otokens()
    except Exception:
        logger.exception("Failed to discover oTokens from factory")
        return {}

    if not otokens:
        logger.debug("No active oTokens on-chain; otoken_address will be null")
        return {}

    result: dict[tuple, str] = {}
    for q in quotes:
        q_type = q.option_type.value
        best_addr = None
        best_expiry_diff = float("inf")
        for ot in otokens:
            ot_type = "put" if ot["is_put"] else "call"
            if ot_type != q_type:
                continue
            if abs(q.strike - ot["strike_usd"]) > 1.0:
                continue
            diff = abs(q.expiry_days - ot["expiry_days"])
            if diff < best_expiry_diff and diff <= MAX_EXPIRY_DIFF_DAYS:
                best_expiry_diff = diff
                best_addr = ot["address"]
        if best_addr:
            result[_quote_key(q)] = best_addr

    return result


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

    otoken_map = await asyncio.to_thread(_build_otoken_map, quotes)

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
            otoken_address=otoken_map.get(_quote_key(q)),
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
