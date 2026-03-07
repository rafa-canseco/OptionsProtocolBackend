import logging
import math
import re
import time
from datetime import datetime, timezone

from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request

from src.config import settings
from src.db.database import get_client
from src.models.price import PriceResponse
from src.models.waitlist import WaitlistRequest, WaitlistResponse
from src.pricing.circuit_breaker import circuit_breaker

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
logger = logging.getLogger(__name__)

router = APIRouter()

USDC_DECIMALS = 6
OTOKEN_DECIMALS = 8

# --- Caches ---
_PRICES_TTL = 15  # seconds
_prices_cache: list | None = None
_prices_cached_at: float = 0.0

# --- Waitlist rate limit (in-memory, per IP) ---
_WAITLIST_WINDOW = 60  # seconds
_WAITLIST_MAX_REQUESTS = 5
_WAITLIST_MAX_TRACKED_IPS = 10_000
_waitlist_hits: dict[str, list[float]] = defaultdict(list)

# --- Read endpoint rate limit (positions, waitlist/count) ---
_READ_WINDOW = 60  # seconds
_READ_MAX_REQUESTS = 30  # allows 1 req/2s; frontend polls /positions every 10s
_read_hits: dict[str, list[float]] = defaultdict(list)


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
        stale = [
            k
            for k, v in _waitlist_hits.items()
            if not v or now - v[-1] >= _WAITLIST_WINDOW
        ]
        for k in stale:
            del _waitlist_hits[k]

    hits = _waitlist_hits[ip]
    _waitlist_hits[ip] = [t for t in hits if now - t < _WAITLIST_WINDOW]
    if len(_waitlist_hits[ip]) >= _WAITLIST_MAX_REQUESTS:
        logger.warning("Rate limit exceeded for IP %s", ip)
        raise HTTPException(
            status_code=429, detail="Too many requests, try again later"
        )
    _waitlist_hits[ip].append(now)


def _check_read_rate_limit(ip: str) -> None:
    """Raise 429 if ip exceeded _READ_MAX_REQUESTS in the last window."""
    now = time.monotonic()

    if len(_read_hits) > _WAITLIST_MAX_TRACKED_IPS:
        stale = [k for k, v in _read_hits.items() if not v or now - v[-1] >= _READ_WINDOW]
        for k in stale:
            del _read_hits[k]

    hits = _read_hits[ip]
    _read_hits[ip] = [t for t in hits if now - t < _READ_WINDOW]
    if len(_read_hits[ip]) >= _READ_MAX_REQUESTS:
        logger.warning("Read rate limit exceeded for IP %s", ip)
        raise HTTPException(
            status_code=429, detail="Too many requests, try again later"
        )
    _read_hits[ip].append(now)


def _fetch_active_quotes() -> list[dict]:
    """Read all active, non-expired quotes from mm_quotes.

    Excludes quotes whose oToken expiry is within 48h of now so that
    near-expiry options are never shown even if the DB has stale rows.
    """
    now_ts = int(time.time())
    expiry_cutoff_ts = now_ts + 48 * 3600
    client = get_client()
    result = (
        client.table("mm_quotes")
        .select("*")
        .eq("is_active", True)
        .gt("deadline", now_ts)
        .gt("expiry", expiry_cutoff_ts)
        .execute()
    )
    return result.data or []


def _best_quotes_by_otoken(quotes: list[dict]) -> list[dict]:
    """For each oToken, pick the quote with the highest bid price.

    When users sell options, they want the highest premium — so we pick
    the MM offering the best (highest) bid for each oToken.
    """
    by_otoken: dict[str, dict] = {}
    for q in quotes:
        try:
            otoken = q["otoken_address"]
            bid = float(q["bid_price"])
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed quote %s: %s", q.get("id"), e)
            continue
        if otoken not in by_otoken or bid > float(by_otoken[otoken]["bid_price"]):
            by_otoken[otoken] = q
    return list(by_otoken.values())


def _quote_to_price_response(q: dict) -> PriceResponse | None:
    """Convert a mm_quotes DB row to a PriceResponse for the frontend."""
    try:
        bid_price_raw = int(q["bid_price"])
        max_amount_raw = int(q["max_amount"])
        deadline = q["deadline"]
        strike = q.get("strike_price")
        expiry = q.get("expiry")
        is_put = q.get("is_put")

        # Compute human-readable fields
        premium_usd = bid_price_raw / (10**USDC_DECIMALS)
        # Apply protocol fee (same as before: user sees net premium)
        fee_mult = (10_000 - settings.protocol_fee_bps) / 10_000
        net_premium = premium_usd * fee_mult

        available_eth = max_amount_raw / (10**OTOKEN_DECIMALS)

        # Compute expiry_days (cosmetic) and expiry_date (stable)
        now_ts = int(time.time())
        expiry_days = max(1, math.ceil((expiry - now_ts) / 86400)) if expiry else 0
        expiry_date = (
            datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%Y-%m-%d")
            if expiry
            else None
        )

        # TTL = seconds until deadline
        ttl = max(0, deadline - now_ts)

        from src.pricing.black_scholes import OptionType

        option_type = OptionType.PUT if is_put else OptionType.CALL

        return PriceResponse(
            option_type=option_type,
            strike=strike or 0,
            expiry_days=expiry_days,
            expiry_date=expiry_date,
            premium=net_premium,
            delta=0,  # Not available from MM quotes
            iv=0,  # Not available from MM quotes
            spot=0,  # Will be enriched below if possible
            ttl=ttl,
            expires_at=float(deadline),
            available_amount=available_eth,
            otoken_address=q["otoken_address"],
            signature=q["signature"],
            mm_address=q["mm_address"],
            bid_price_raw=bid_price_raw,
            deadline=deadline,
            quote_id=q["quote_id"],
            max_amount_raw=max_amount_raw,
            maker_nonce=q["maker_nonce"],
        )
    except Exception:
        logger.exception("Failed to convert quote to PriceResponse: %s", q.get("id"))
        return None


@router.get(
    "/prices",
    response_model=list[PriceResponse],
    tags=["Market Data"],
    summary="Get current option price menu",
)
async def get_prices():
    """Return the live ETH options price sheet.

    Reads all active signed quotes from market makers, picks the best
    bid for each oToken, and returns enriched PriceResponse objects.
    The response includes EIP-712 signature data needed by the frontend
    to call executeOrder on BatchSettler.

    Returns **503** if the circuit breaker has paused pricing (>2 % ETH move).
    """
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

    logger.info("prices cache miss — fetching from mm_quotes")

    try:
        all_quotes = _fetch_active_quotes()
    except Exception:
        logger.exception("Failed to fetch active quotes from DB")
        raise HTTPException(502, "Quote data unavailable")

    if not all_quotes:
        logger.info("No active quotes in mm_quotes")
        _prices_cache = []
        _prices_cached_at = time.monotonic()
        return []

    best_quotes = _best_quotes_by_otoken(all_quotes)

    # Enrich with spot price if available (best effort)
    spot = 0.0
    try:
        from src.pricing.chainlink import get_eth_price

        spot, _ = get_eth_price()
        if circuit_breaker.check(spot):
            raise HTTPException(
                status_code=503,
                detail=f"Pricing paused: {circuit_breaker.pause_reason}",
            )
        circuit_breaker.update_reference(spot)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Could not fetch spot price for enrichment", exc_info=True)

    result = []
    for q in best_quotes:
        pr = _quote_to_price_response(q)
        if pr is not None:
            if spot > 0:
                pr.spot = spot
            result.append(pr)

    _prices_cache = result
    _prices_cached_at = time.monotonic()
    return result


@router.post(
    "/waitlist",
    response_model=WaitlistResponse,
    tags=["Waitlist"],
    summary="Join the waitlist",
)
async def join_waitlist(body: WaitlistRequest, request: Request):
    """Add an email to the b1nary waitlist.

    Idempotent — submitting the same email twice returns 200 with `new: false`.
    Rate-limited to 5 requests per IP per 60 s window.
    """
    _check_rate_limit(_get_client_ip(request))
    client = get_client()
    try:
        existing = (
            client.table("waitlist").select("id").eq("email", body.email).execute()
        )
        is_new = not existing.data
    except Exception:
        logger.exception("Waitlist existence check failed")
        raise HTTPException(status_code=502, detail="Could not save to waitlist")
    try:
        result = (
            client.table("waitlist")
            .upsert(
                {"email": body.email},
                on_conflict="email",
            )
            .execute()
        )
    except Exception:
        logger.exception("Waitlist upsert failed")
        raise HTTPException(status_code=502, detail="Could not save to waitlist")
    if not result.data:
        logger.error("Waitlist upsert returned empty data")
        raise HTTPException(status_code=502, detail="Could not save to waitlist")
    return WaitlistResponse(ok=True, new=is_new)


@router.get(
    "/waitlist/count",
    tags=["Waitlist"],
    summary="Get waitlist size",
)
async def get_waitlist_count(request: Request):
    """Return `{\"count\": N}` with the total number of emails on the waitlist."""
    _check_read_rate_limit(_get_client_ip(request))
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


@router.get(
    "/positions/{address}",
    tags=["Positions"],
    summary="Get positions for a wallet",
)
async def get_positions(address: str, request: Request):
    """Return all option positions for the given Ethereum address.

    Data comes from on-chain `OrderExecuted` events indexed into Supabase.
    Each position includes strike, expiry, premium paid, settlement status,
    and a human-readable `outcome` field for settled positions.
    """
    _check_read_rate_limit(_get_client_ip(request))
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
