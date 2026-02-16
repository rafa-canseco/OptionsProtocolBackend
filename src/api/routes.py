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
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _quote_key(q: PriceQuote) -> tuple:
    """Key to match a BS quote to an oToken: (type, strike, expiry_days)."""
    return (q.option_type.value, q.strike, q.expiry_days)


def _build_otoken_map(quotes: list[PriceQuote]) -> dict[tuple, str]:
    """Map (type, strike, expiry_days) → oToken address via exact hash lookup.

    Uses the same params hash as the contracts (keccak256 of packed params)
    to look up oToken addresses from OTokenFactory. Read-only — does not create.
    Returns empty dict on failure.
    """
    try:
        from src.bots.price_publisher import (
            compute_params_hash,
            strike_to_8_decimals,
            expiry_days_to_timestamp,
        )
        from src.pricing.black_scholes import OptionType
        from src.contracts.web3_client import get_otoken_factory
        from src.config import settings
        from web3 import Web3

        factory = get_otoken_factory()
        weth = Web3.to_checksum_address(settings.weth_address)
        usdc = Web3.to_checksum_address(settings.usdc_address)
    except Exception:
        logger.exception("Failed to initialize oToken lookup")
        return {}

    result: dict[tuple, str] = {}
    seen: dict[tuple, str] = {}

    for q in quotes:
        key = _quote_key(q)
        if key in seen:
            result[key] = seen[key]
            continue

        is_put = q.option_type == OptionType.PUT
        collateral = usdc if is_put else weth
        strike_price = strike_to_8_decimals(q.strike)
        expiry = expiry_days_to_timestamp(q.expiry_days)

        try:
            params_hash = compute_params_hash(weth, usdc, collateral, strike_price, expiry, is_put)
            addr = factory.functions.getOToken(params_hash).call()
        except Exception:
            logger.debug(f"Failed to look up oToken for {key}")
            seen[key] = ""
            continue

        if addr != ZERO_ADDRESS:
            result[key] = addr
            seen[key] = addr
        else:
            seen[key] = ""

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
