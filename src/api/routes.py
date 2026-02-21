import asyncio
import logging
import re
import time

from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request

from src.config import settings
from src.db.database import get_client
from src.models.price import PriceResponse
from src.models.waitlist import WaitlistRequest, WaitlistResponse
from src.pricing.chainlink import get_eth_price
from src.pricing.circuit_breaker import circuit_breaker
from src.pricing.deribit import get_eth_iv
from src.pricing.price_sheet import PriceQuote, generate_price_sheet

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_AVAILABLE_AMOUNT = settings.default_max_amount_wei / 10**18
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# --- Caches ---
_PRICES_TTL = 15  # seconds
_prices_cache: list | None = None
_prices_cached_at: float = 0.0

_OTOKEN_TTL = 120  # seconds
_otoken_cache: dict[tuple, str] = {}
_otoken_cached_at: float = 0.0
_otoken_quote_keys: set[tuple] | None = None

# --- Waitlist rate limit (in-memory, per IP) ---
_WAITLIST_WINDOW = 60  # seconds
_WAITLIST_MAX_REQUESTS = 5
_WAITLIST_MAX_TRACKED_IPS = 10_000
_waitlist_hits: dict[str, list[float]] = defaultdict(list)


def _get_client_ip(request: Request) -> str:
    """Extract client IP, preferring X-Forwarded-For for proxied requests."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _check_rate_limit(ip: str) -> None:
    """Raise 429 if ip exceeded _WAITLIST_MAX_REQUESTS in the last window."""
    now = time.monotonic()

    if len(_waitlist_hits) > _WAITLIST_MAX_TRACKED_IPS:
        stale = [k for k, v in _waitlist_hits.items()
                 if not v or now - v[-1] >= _WAITLIST_WINDOW]
        for k in stale:
            del _waitlist_hits[k]

    hits = _waitlist_hits[ip]
    _waitlist_hits[ip] = [t for t in hits if now - t < _WAITLIST_WINDOW]
    if len(_waitlist_hits[ip]) >= _WAITLIST_MAX_REQUESTS:
        logger.warning("Rate limit exceeded for IP %s", ip)
        raise HTTPException(status_code=429, detail="Too many requests, try again later")
    _waitlist_hits[ip].append(now)


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
    # Cache lookups: None = failed/not found, str = address
    seen: dict[tuple, str | None] = {}

    for q in quotes:
        key = _quote_key(q)
        if key in seen:
            if seen[key] is not None:
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
            logger.exception(f"Failed to look up oToken for {key}")
            seen[key] = None
            continue

        if addr != ZERO_ADDRESS:
            result[key] = addr
            seen[key] = addr
        else:
            seen[key] = None

    return result


async def _get_otoken_map(quotes: list[PriceQuote]) -> dict[tuple, str]:
    """Return otoken_map, using a 120s cache that invalidates when quotes change."""
    global _otoken_cache, _otoken_cached_at, _otoken_quote_keys

    current_keys = {_quote_key(q) for q in quotes}
    now = time.monotonic()
    cache_valid = (
        (now - _otoken_cached_at) < _OTOKEN_TTL
        and _otoken_quote_keys == current_keys
    )
    if cache_valid:
        logger.debug("otoken_map cache hit (age=%.1fs)", now - _otoken_cached_at)
        return _otoken_cache

    logger.info("otoken_map cache miss — refreshing")
    otoken_map = await asyncio.to_thread(_build_otoken_map, quotes)

    if not otoken_map and quotes:
        logger.warning("otoken_map empty for %d quotes — not caching", len(quotes))
        return _otoken_cache or otoken_map

    _otoken_cache = otoken_map
    _otoken_cached_at = now
    _otoken_quote_keys = current_keys
    return otoken_map


@router.get("/prices", response_model=list[PriceResponse])
async def get_prices():
    """Get current price menu for ETH options."""
    global _prices_cache, _prices_cached_at

    if circuit_breaker.is_paused:
        raise HTTPException(
            status_code=503,
            detail=f"Pricing paused: {circuit_breaker.pause_reason}",
        )

    now = time.monotonic()
    if _prices_cache is not None and (now - _prices_cached_at) < _PRICES_TTL:
        logger.debug("prices cache hit (age=%.1fs)", now - _prices_cached_at)
        return _prices_cache

    logger.info("prices cache miss — recalculating")

    # Parallelize Chainlink (sync, in thread) and Deribit (async)
    try:
        (eth_price, _), iv = await asyncio.gather(
            asyncio.to_thread(get_eth_price),
            get_eth_iv(),
        )
    except Exception:
        logger.exception("Failed to fetch market data from Chainlink/Deribit")
        raise HTTPException(502, "Market data unavailable — Chainlink or Deribit may be down")

    if circuit_breaker.check(eth_price):
        raise HTTPException(
            status_code=503,
            detail=f"Pricing paused: {circuit_breaker.pause_reason}",
        )

    circuit_breaker.update_reference(eth_price)
    quotes = generate_price_sheet(spot=eth_price, iv=iv)

    otoken_map = await _get_otoken_map(quotes)

    if not (0 <= settings.protocol_fee_bps < 10_000):
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: invalid protocol_fee_bps",
        )
    fee_mult = (10_000 - settings.protocol_fee_bps) / 10_000
    result = [
        PriceResponse(
            option_type=q.option_type,
            strike=q.strike,
            expiry_days=q.expiry_days,
            premium=q.premium * fee_mult,
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

    _prices_cache = result
    _prices_cached_at = time.monotonic()
    return result


@router.post("/waitlist", response_model=WaitlistResponse)
async def join_waitlist(body: WaitlistRequest, request: Request):
    """Add an email to the waitlist. Idempotent — duplicates return 200."""
    _check_rate_limit(_get_client_ip(request))
    client = get_client()
    try:
        existing = client.table("waitlist").select("id").eq("email", body.email).execute()
        is_new = not existing.data
    except Exception:
        logger.exception("Waitlist existence check failed")
        raise HTTPException(status_code=502, detail="Could not save to waitlist")
    try:
        result = client.table("waitlist").upsert(
            {"email": body.email},
            on_conflict="email",
        ).execute()
    except Exception:
        logger.exception("Waitlist upsert failed")
        raise HTTPException(status_code=502, detail="Could not save to waitlist")
    if not result.data:
        logger.error("Waitlist upsert returned empty data")
        raise HTTPException(status_code=502, detail="Could not save to waitlist")
    return WaitlistResponse(ok=True, new=is_new)


@router.get("/waitlist/count")
async def get_waitlist_count():
    """Return the number of emails on the waitlist."""
    client = get_client()
    try:
        result = client.table("waitlist").select("id", count="exact").execute()
        count = result.count
    except Exception:
        logger.exception("Waitlist count failed")
        raise HTTPException(status_code=502, detail="Could not fetch waitlist count")
    if count is None:
        logger.error("Waitlist count returned None")
        raise HTTPException(status_code=502, detail="Could not fetch waitlist count")
    return {"count": count}


def _compute_outcome(position: dict) -> str | None:
    """Compute human-readable outcome for settled positions.

    Examples:
      - "Bought 1.0000 ETH @ $2,400" — PUT ITM, user's USDC collateral was
        swapped to WETH at strike (physical delivery)
      - "Sold 1.0000 ETH @ $2,800" — CALL ITM, user's WETH collateral was
        swapped to USDC at strike (physical delivery)
      - "Expired ITM — cash settled" — physical delivery failed, fallback
      - "Expired OTM — collateral returned"
    """
    if not position.get("is_settled"):
        return None

    if position.get("is_itm"):
        st = position.get("settlement_type")
        if st == "physical":
            strike = position.get("strike_price")
            amount_raw = position.get("amount")
            is_put = position.get("is_put")
            if strike is None or amount_raw is None or is_put is None:
                return "Settled (physical) — details unavailable"
            try:
                # Both oToken amount and strike_price use 8 decimals
                amount_human = int(amount_raw) / 1e8
                strike_human = int(strike) / 1e8
            except (ValueError, TypeError):
                return "Settled (physical) — details unavailable"
            if is_put:
                return f"Bought {amount_human:.4f} ETH @ ${strike_human:,.0f}"
            else:
                return f"Sold {amount_human:.4f} ETH @ ${strike_human:,.0f}"
        elif st == "physical_failed":
            return "Expired ITM — delivery failed, pending review"
        else:
            return "Expired ITM — cash settled"

    return "Expired OTM — collateral returned"


@router.get("/positions/{address}")
async def get_positions(address: str):
    """Get all positions for a user address (from indexed on-chain events)."""
    if not ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")

    try:
        client = get_client()
        result = (
            client.table("order_events")
            .select("*")
            .eq("user_address", address.lower())
            .order("indexed_at", desc=True)
            .execute()
        )
    except Exception:
        logger.exception(f"Failed to fetch positions for {address}")
        raise HTTPException(status_code=502, detail="Could not fetch positions")

    positions = result.data or []
    for pos in positions:
        pos["outcome"] = _compute_outcome(pos)
        # Frontend sees net_premium as "premium". Fall back to premium
        # for old rows that predate the fee columns.
        if pos.get("net_premium") is not None:
            pos["premium"] = pos["net_premium"]
    return positions
