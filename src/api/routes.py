import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone

from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from src.config import settings
from src.db.database import get_client
from src.models.mm import CapacityResponse
from src.models.price import PriceResponse
from src.models.waitlist import WaitlistRequest, WaitlistResponse
from src.chains import Chain
from src.chains.address import detect_chain, ETH_ADDRESS_RE, is_valid_solana_address
from src.chains.explorer import tx_explorer_url
from src.pricing.assets import Asset, get_chain_for_asset
from src.pricing.circuit_breaker import circuit_breaker

logger = logging.getLogger(__name__)

router = APIRouter()

USDC_DECIMALS = 6
OTOKEN_DECIMALS = 8

# --- Caches (per asset) ---
_PRICES_TTL = 15  # seconds
_prices_cache: dict[str, list] = {}
_prices_cached_at: dict[str, float] = {}

# --- In-memory rate limiting (per IP, per worker process) ---
# NOTE: State is not shared across uvicorn workers. In a multi-worker deployment
# the effective limit is _MAX_REQUESTS * num_workers per IP per window.
# For hard per-IP enforcement on mainnet, replace with a shared Redis store.
_MAX_TRACKED_IPS = 10_000  # eviction threshold shared by all rate limiters

_WAITLIST_WINDOW = 60  # seconds
_WAITLIST_MAX_REQUESTS = 5
_waitlist_hits: dict[str, list[float]] = defaultdict(list)

_READ_WINDOW = 60  # seconds
_READ_MAX_REQUESTS = 30  # allows 1 req/2s; frontend polls /positions every 10s
_read_hits: dict[str, list[float]] = defaultdict(list)

_CAPACITY_STALE_SECONDS = 120  # MM reports every ~30s; 2min = stale

# Minimum seconds of deadline remaining for a quote to be served.
# Matches _PRICES_TTL so a cached response never contains an
# already-expired quote. The MM controls user-facing time via deadline.
_MIN_QUOTE_TTL = _PRICES_TTL

ACTIVITY_MULTIPLIER = 1


def _get_client_ip(request: Request) -> str:
    """Extract client IP, preferring X-Forwarded-For for proxied requests."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    logger.warning(
        "Could not determine client IP; all such requests share one rate-limit bucket"
    )
    return "unknown"


def _check_rate_limit(ip: str) -> None:
    """Raise 429 if ip exceeded _WAITLIST_MAX_REQUESTS in the last window."""
    now = time.monotonic()

    if len(_waitlist_hits) > _MAX_TRACKED_IPS:
        stale = [
            k
            for k, v in _waitlist_hits.items()
            if not v or now - v[-1] >= _WAITLIST_WINDOW
        ]
        for k in stale:
            del _waitlist_hits[k]
        if len(_waitlist_hits) > _MAX_TRACKED_IPS:
            logger.warning(
                "Waitlist rate limiter: %d IPs tracked (over %d limit), no stale entries to evict",
                len(_waitlist_hits),
                _MAX_TRACKED_IPS,
            )

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

    if len(_read_hits) > _MAX_TRACKED_IPS:
        stale = [
            k for k, v in _read_hits.items() if not v or now - v[-1] >= _READ_WINDOW
        ]
        for k in stale:
            del _read_hits[k]
        if len(_read_hits) > _MAX_TRACKED_IPS:
            logger.warning(
                "Read rate limiter: %d IPs tracked (over %d limit), no stale entries to evict",
                len(_read_hits),
                _MAX_TRACKED_IPS,
            )

    hits = _read_hits[ip]
    _read_hits[ip] = [t for t in hits if now - t < _READ_WINDOW]
    if len(_read_hits[ip]) >= _READ_MAX_REQUESTS:
        logger.warning("Read rate limit exceeded for IP %s", ip)
        raise HTTPException(
            status_code=429, detail="Too many requests, try again later"
        )
    _read_hits[ip].append(now)


def _fetch_capacity_rows(asset: Asset = Asset.ETH) -> list[dict]:
    """Read non-stale mm_capacity rows from Supabase, filtered by asset."""
    cutoff = datetime.fromtimestamp(
        time.time() - _CAPACITY_STALE_SECONDS, tz=timezone.utc
    ).isoformat()
    chain = get_chain_for_asset(asset).value
    client = get_client()
    result = (
        client.table("mm_capacity")
        .select("*")
        .eq("asset", asset.value)
        .eq("chain", chain)
        .gte("reported_at", cutoff)
        .execute()
    )
    if result.data is None:
        raise RuntimeError("mm_capacity query returned None data")
    return result.data


def _aggregate_capacity(rows: list[dict], asset: Asset = Asset.ETH) -> dict:
    """Aggregate capacity rows into a single summary."""
    if not rows:
        return {
            "asset": asset.value,
            "capacity": 0.0,
            "capacity_usd": 0.0,
            "market_open": False,
            "market_status": "full",
            "max_position": 0.0,
            "mm_count": 0,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    total_native = 0.0
    total_usd = 0.0
    max_single = 0.0
    any_active = False
    any_degraded = False
    latest_at = ""
    parsed_count = 0

    for r in rows:
        try:
            native = float(r["capacity_eth"])
            usd = float(r["capacity_usd"])
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                "Skipping malformed capacity row for %s: %s",
                r.get("mm_address", "unknown"),
                e,
            )
            continue
        parsed_count += 1
        status = r.get("status", "active")
        if status == "full":
            pass  # count for status logic but don't add capacity
        else:
            total_native += native
            total_usd += usd
            max_single = max(max_single, native)
        if status == "active":
            any_active = True
        elif status == "degraded":
            any_degraded = True
        reported = r.get("reported_at", "")
        if reported > latest_at:
            latest_at = reported

    if any_active:
        market_status = "active"
    elif any_degraded:
        market_status = "degraded"
    else:
        market_status = "full"

    return {
        "asset": asset.value,
        "capacity": total_native,
        "capacity_usd": total_usd,
        "market_open": market_status != "full",
        "market_status": market_status,
        "max_position": max_single,
        "mm_count": parsed_count,
        "updated_at": latest_at,
    }


@router.get(
    "/capacity",
    response_model=CapacityResponse,
    tags=["Market Data"],
    summary="Get available market capacity",
)
async def get_capacity(
    asset: Asset = Query(default=Asset.ETH, description="Underlying asset"),
):
    """Return aggregated capacity across all active market makers.

    Capacity is considered stale if not reported within 120 seconds.
    """
    try:
        rows = _fetch_capacity_rows(asset)
    except Exception:
        logger.exception("Failed to fetch mm_capacity")
        raise HTTPException(502, "Capacity data unavailable")

    return _aggregate_capacity(rows, asset)


def _fetch_valid_otoken_addresses(asset: Asset) -> set[str] | None:
    """Return set of otoken_addresses in available_otokens, or None on error."""
    try:
        chain = get_chain_for_asset(asset).value
        client = get_client()
        result = (
            client.table("available_otokens")
            .select("otoken_address")
            .eq("chain", chain)
            .execute()
        )
        return {r["otoken_address"] for r in (result.data or [])}
    except Exception:
        logger.warning(
            "Could not fetch available_otokens for %s, skipping filter",
            asset.value,
            exc_info=True,
        )
        return None


def _fetch_active_quotes(asset: Asset = Asset.ETH) -> list[dict]:
    """Read active, non-expired quotes from mm_quotes for a given asset.

    Uses a dynamic cutoff: short-term expiries (TTL <= 48h) get a 4h
    cutoff, standard expiries get 48h. SQL uses the minimum cutoff (4h)
    to cast a wide net, then Python applies per-quote dynamic cutoff.
    """
    from src.pricing.utils import cutoff_hours_for_expiry

    now_ts = int(time.time())
    min_cutoff_ts = now_ts + settings.short_expiry_cutoff_hours * 3600
    chain = get_chain_for_asset(asset).value
    client = get_client()
    result = (
        client.table("mm_quotes")
        .select("*")
        .eq("is_active", True)
        .eq("asset", asset.value)
        .eq("chain", chain)
        .gt("deadline", now_ts + _MIN_QUOTE_TTL)
        .gt("expiry", min_cutoff_ts)
        .execute()
    )
    quotes = result.data or []

    # Apply per-quote dynamic cutoff
    filtered = []
    for q in quotes:
        expiry = q.get("expiry", 0)
        cutoff_h = cutoff_hours_for_expiry(expiry, now_ts)
        if expiry > now_ts + cutoff_h * 3600:
            filtered.append(q)
    return filtered


def _best_quotes_by_otoken(quotes: list[dict]) -> list[dict]:
    """For each (strike, expiry, is_put), pick the quote with the highest bid.

    Deduplicates by option identity rather than oToken address to handle
    cases where multiple oTokens exist for the same strike/expiry/type
    (e.g. after a factory upgrade).
    """
    by_option: dict[tuple, dict] = {}
    for q in quotes:
        try:
            bid = float(q["bid_price"])
            strike = q.get("strike_price")
            expiry = q.get("expiry")
            is_put = q.get("is_put")
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed quote %s: %s", q.get("id"), e)
            continue
        key = (strike, expiry, is_put)
        if key not in by_option or bid > float(by_option[key]["bid_price"]):
            by_option[key] = q
    return list(by_option.values())


def _fetch_position_counts(asset: Asset) -> dict[tuple, int]:
    """Count active positions per (strike_usd, is_put, expiry) for the asset.

    Active = not settled and not expired. Returns empty dict on any failure
    so callers can default position_count to 0 without surfacing the error.
    """
    now_ts = int(time.time())
    chain = get_chain_for_asset(asset).value
    try:
        client = get_client()
        result = (
            client.table("order_events")
            .select("strike_price,is_put,expiry")
            .eq("asset", asset.value)
            .eq("chain", chain)
            .or_("is_settled.eq.false,is_settled.is.null")
            .gt("expiry", now_ts)
            .execute()
        )
        rows = result.data or []
    except Exception:
        logger.warning(
            "Failed to fetch position counts; defaulting to 0", exc_info=True
        )
        return {}

    counts: dict[tuple, int] = {}
    for row in rows:
        try:
            strike_usd = float(row["strike_price"]) / 1e8
            is_put = row["is_put"]
            expiry = row["expiry"]
        except (KeyError, ValueError, TypeError):
            continue
        key = (strike_usd, is_put, expiry)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _quote_to_price_response(q: dict) -> PriceResponse | None:
    """Convert a mm_quotes DB row to a PriceResponse for the frontend."""
    try:
        bid_price_raw = int(q["bid_price"])
        max_amount_raw = int(q["max_amount"])
        deadline = q["deadline"]
        strike = q.get("strike_price")
        expiry = q.get("expiry")
        is_put = q.get("is_put")
        chain = q.get("chain", "base")

        # BatchSettler treats bid_price as USDC smallest units on every chain.
        premium_usd = bid_price_raw / (10**USDC_DECIMALS)
        fee_mult = (10_000 - settings.protocol_fee_bps) / 10_000
        net_premium = premium_usd * fee_mult

        available_eth = max_amount_raw / (10**OTOKEN_DECIMALS)

        now_ts = int(time.time())
        expiry_days = max(1, math.ceil((expiry - now_ts) / 86400)) if expiry else 0
        expiry_date = (
            datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%Y-%m-%d")
            if expiry
            else None
        )

        ttl = max(0, deadline - now_ts)

        from src.pricing.black_scholes import OptionType

        option_type = OptionType.PUT if is_put else OptionType.CALL

        return PriceResponse(
            option_type=option_type,
            strike=strike or 0,
            expiry_days=expiry_days,
            expiry_date=expiry_date,
            premium=net_premium,
            delta=0,
            iv=0,
            spot=0,
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
            chain=chain,
        )
    except Exception:
        logger.exception(
            "Failed to convert quote to PriceResponse: id=%s chain=%s",
            q.get("id"),
            q.get("chain", "unknown"),
        )
        return None


@router.get(
    "/spot",
    tags=["Market Data"],
    summary="Get current spot price for an asset",
)
async def get_spot(
    asset: Asset = Query(default=Asset.ETH, description="Underlying asset"),
):
    """Return the live spot price for a given asset (Chainlink or Pyth)."""
    chain = get_chain_for_asset(asset)

    try:
        if chain == Chain.SOLANA:
            from src.chains.solana.oracle import get_spot_price

            price, updated_at = get_spot_price(asset)
        else:
            from src.pricing.chainlink import get_asset_price

            price, updated_at = get_asset_price(asset)
    except Exception:
        logger.exception("Failed to fetch %s spot price", asset.value)
        raise HTTPException(502, f"Could not fetch {asset.value.upper()} spot")

    return {
        "asset": asset.value,
        "spot": price,
        "updated_at": updated_at,
    }


@router.get(
    "/prices",
    response_model=list[PriceResponse],
    tags=["Market Data"],
    summary="Get current option price menu",
)
async def get_prices(
    asset: Asset = Query(default=Asset.ETH, description="Underlying asset"),
):
    """Return the live options price sheet for a given asset.

    Reads all active signed quotes from market makers, picks the best
    bid for each oToken, and returns enriched PriceResponse objects.
    The response includes EIP-712 signature data needed by the frontend
    to call executeOrder on BatchSettler.

    Returns **503** only if the circuit breaker has paused pricing (>2 % move).
    Capacity status is served separately via ``GET /capacity``.
    """
    # Fetch spot early so we can self-heal a paused circuit breaker.
    spot = 0.0
    spot_ok = False
    try:
        chain = get_chain_for_asset(asset)
        if chain == Chain.SOLANA:
            from src.chains.solana.oracle import get_spot_price

            spot, _ = get_spot_price(asset)
        else:
            from src.pricing.chainlink import get_asset_price

            spot, _ = get_asset_price(asset)
        spot_ok = True
    except Exception:
        logger.warning("Could not fetch spot price for enrichment", exc_info=True)

    if circuit_breaker.is_paused_for(asset.value):
        if spot_ok:
            circuit_breaker.resume(spot, asset.value)
            logger.info(
                "Circuit breaker auto-resumed for %s at $%.2f",
                asset.value,
                spot,
            )
        else:
            raise HTTPException(
                status_code=503,
                detail=f"Pricing paused: {circuit_breaker.pause_reason_for(asset.value)}",
            )

    cache_key = asset.value
    now = time.monotonic()
    cached = _prices_cache.get(cache_key)
    cached_at = _prices_cached_at.get(cache_key, 0.0)
    if cached is not None and (now - cached_at) < _PRICES_TTL:
        logger.debug("prices cache hit for %s (age=%.1fs)", cache_key, now - cached_at)
        return cached

    logger.info("prices cache miss for %s — fetching from mm_quotes", cache_key)

    try:
        all_quotes = _fetch_active_quotes(asset)
    except Exception:
        logger.exception("Failed to fetch active quotes from DB")
        raise HTTPException(502, "Quote data unavailable")

    if not all_quotes:
        logger.info("No active quotes in mm_quotes for %s", cache_key)
        return []

    # Filter quotes to only those with oTokens in available_otokens
    valid_addrs = _fetch_valid_otoken_addresses(asset)
    if valid_addrs is not None:
        before = len(all_quotes)
        all_quotes = [q for q in all_quotes if q.get("otoken_address") in valid_addrs]
        pruned = before - len(all_quotes)
        if pruned:
            logger.info(
                "Filtered %d stale quotes (oToken not in available_otokens) for %s",
                pruned,
                cache_key,
            )
        if not all_quotes:
            return []

    best_quotes = _best_quotes_by_otoken(all_quotes)

    # Check circuit breaker with fresh spot
    if spot_ok:
        if circuit_breaker.check(spot, asset.value):
            raise HTTPException(
                status_code=503,
                detail=f"Pricing paused: {circuit_breaker.pause_reason_for(asset.value)}",
            )
        circuit_breaker.update_reference(spot, asset.value)

    # Fetch position counts for social proof (best effort)
    position_counts: dict[tuple, int] = {}
    try:
        position_counts = _fetch_position_counts(asset)
    except Exception:
        logger.warning("Could not enrich position counts", exc_info=True)

    from src.pricing.black_scholes import OptionType

    result = []
    visible_keys: dict[tuple, int] = {}  # (strike, is_put, expiry) -> index
    for q in best_quotes:
        pr = _quote_to_price_response(q)
        if pr is not None:
            if spot > 0:
                pr.spot = spot
            idx = len(result)
            result.append(pr)
            is_put = pr.option_type == OptionType.PUT
            visible_keys[(pr.strike, is_put, q.get("expiry"))] = idx

    # Rollup: assign each position group to its visible key, or roll
    # orphaned positions (e.g. within 48h cutoff) into the nearest
    # visible expiry for the same (strike, option_type).
    merged = [0] * len(result)
    for (strike, is_put, expiry), count in position_counts.items():
        if (strike, is_put, expiry) in visible_keys:
            merged[visible_keys[(strike, is_put, expiry)]] += count
        else:
            candidates = [
                (vis_exp, idx)
                for (s, p, vis_exp), idx in visible_keys.items()
                if s == strike and p == is_put
            ]
            if candidates:
                nearest_idx = min(candidates, key=lambda x: abs(x[0] - expiry))[1]
                merged[nearest_idx] += count

    for i, pr in enumerate(result):
        pr.position_count = merged[i] * ACTIVITY_MULTIPLIER

    _prices_cache[cache_key] = result
    _prices_cached_at[cache_key] = time.monotonic()
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
            asset_label = position.get("asset", "ETH").upper()
            if is_put:
                return f"Bought {amount_human:.4f} {asset_label} @ ${strike_human:,.0f}"
            else:
                return f"Sold {amount_human:.4f} {asset_label} @ ${strike_human:,.0f}"
        elif st == "physical_failed":
            return "Expired ITM — delivery failed, pending review"
        else:
            return "Expired ITM — cash settled"

    return "Expired OTM — collateral returned"


def _enrich_positions(positions: list[dict]) -> list[dict]:
    """Add display fields and normalize premium field for positions."""
    for pos in positions:
        pos["outcome"] = _compute_outcome(pos)
        if pos.get("net_premium") is not None:
            pos["premium"] = pos["net_premium"]
        url = tx_explorer_url(pos.get("tx_hash"), pos.get("chain", "base"))
        pos["tx_url"] = url
        pos["explorer_url"] = url
        pos["settlement_tx_url"] = tx_explorer_url(
            pos.get("settlement_tx_hash"),
            pos.get("chain", "base"),
        )
        pos["delivery_tx_url"] = tx_explorer_url(
            pos.get("delivery_tx_hash"),
            pos.get("chain", "base"),
        )
    return positions


@router.get(
    "/positions/{address}",
    tags=["Positions"],
    summary="Get positions for a wallet",
)
async def get_positions(address: str, request: Request):
    """Return all option positions for the given wallet address.

    Accepts both EVM (0x hex) and Solana (base58) addresses.
    Data comes from on-chain events indexed into Supabase.
    Each position includes strike, expiry, premium paid, settlement status,
    and a human-readable `outcome` field for settled positions.
    """
    _check_read_rate_limit(_get_client_ip(request))

    try:
        chain = detect_chain(address)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid address. Expected 0x hex (Base) or base58 (Solana).",
        )

    addr_normalized = address.lower() if chain == Chain.BASE else address

    try:
        client = get_client()
        result = (
            client.table("order_events")
            .select("*")
            .eq("user_address", addr_normalized)
            .eq("chain", chain.value)
            .order("indexed_at", desc=True)
            .execute()
        )
    except Exception:
        logger.exception(f"Failed to fetch positions for {address}")
        raise HTTPException(status_code=502, detail="Could not fetch positions")

    return _enrich_positions(result.data or [])


class GroupPositionsRequest(BaseModel):
    group_id: str
    tx_hashes: list[str]
    user_address: str


@router.post(
    "/positions/group",
    tags=["Positions"],
    summary="Link positions into a range group",
)
async def group_positions(body: GroupPositionsRequest, request: Request):
    """Tag positions with a shared group_id so the frontend can
    display range (put+call) pairs as a single unit.

    The frontend calls this after both legs of a range order confirm.
    """
    _check_read_rate_limit(_get_client_ip(request))

    if len(body.tx_hashes) < 2 or len(body.tx_hashes) > 10:
        raise HTTPException(400, "tx_hashes must contain 2-10 entries")

    try:
        uuid.UUID(body.group_id)
    except ValueError:
        raise HTTPException(400, "group_id must be a valid UUID")

    if not ETH_ADDRESS_RE.match(body.user_address):
        if is_valid_solana_address(body.user_address):
            raise HTTPException(
                400,
                "Position grouping is not yet supported for Solana",
            )
        raise HTTPException(400, "Invalid user_address")

    for tx in body.tx_hashes:
        if not re.match(r"^0x[0-9a-fA-F]{64}$", tx):
            raise HTTPException(400, f"Invalid tx hash: {tx}")

    try:
        client = get_client()
        result = (
            client.table("order_events")
            .update({"group_id": body.group_id})
            .eq("user_address", body.user_address.lower())
            .is_("group_id", "null")
            .in_("tx_hash", [tx.lower() for tx in body.tx_hashes])
            .execute()
        )
        updated = len(result.data) if result.data else 0
    except Exception:
        logger.exception("Failed to group positions")
        raise HTTPException(502, "Could not update positions")

    expected = len(body.tx_hashes)
    if updated == 0:
        raise HTTPException(404, "No matching ungrouped positions found")
    if updated != expected:
        logger.warning(
            "Partial group: expected %d but matched %d (group_id=%s)",
            expected,
            updated,
            body.group_id,
        )
        raise HTTPException(
            409,
            f"Expected {expected} positions but found {updated}. "
            "Some tx hashes may not be indexed yet.",
        )

    return {"grouped": updated, "group_id": body.group_id}


# ── Cross-chain endpoints ─────────────────────────────────────────


@router.get(
    "/positions",
    tags=["Positions"],
    summary="Get positions by Privy user ID (cross-chain)",
)
async def get_positions_by_user(
    request: Request,
    user_id: str = Query(..., description="Privy user ID"),
    base_address: str | None = Query(None, description="User's Base wallet address"),
    solana_address: str | None = Query(
        None, description="User's Solana wallet address"
    ),
):
    """Return positions across both chains for a Privy user.

    The frontend passes the user's wallet addresses per chain
    (from Privy). Returns a unified list with a `chain` field
    on each position.
    """
    _check_read_rate_limit(_get_client_ip(request))

    if not base_address and not solana_address:
        raise HTTPException(
            400, "At least one of base_address or solana_address is required"
        )

    client = get_client()
    positions: list[dict] = []
    errors: list[dict] = []

    if base_address:
        if not ETH_ADDRESS_RE.match(base_address):
            raise HTTPException(400, "Invalid base_address")
        try:
            result = (
                client.table("order_events")
                .select("*")
                .eq("user_address", base_address.lower())
                .eq("chain", "base")
                .order("indexed_at", desc=True)
                .execute()
            )
            positions.extend(_enrich_positions(result.data or []))
        except Exception:
            logger.exception("Failed to fetch Base positions for %s", user_id)
            errors.append({"chain": "base", "message": "Could not load Base positions"})

    if solana_address:
        if not is_valid_solana_address(solana_address):
            raise HTTPException(400, "Invalid solana_address")
        try:
            result = (
                client.table("order_events")
                .select("*")
                .eq("user_address", solana_address)
                .eq("chain", "solana")
                .order("indexed_at", desc=True)
                .execute()
            )
            positions.extend(_enrich_positions(result.data or []))
        except Exception:
            logger.exception("Failed to fetch Solana positions for %s", user_id)
            errors.append(
                {
                    "chain": "solana",
                    "message": "Could not load Solana positions",
                }
            )

    if not positions and errors:
        raise HTTPException(502, "Could not fetch positions from any chain")

    return {"positions": positions, "errors": errors}


@router.get(
    "/balances/{user_id}",
    tags=["Positions"],
    summary="Get cross-chain token balances for a user",
)
async def get_balances(
    user_id: str,
    request: Request,
    base_address: str | None = Query(None, description="User's Base wallet address"),
    solana_address: str | None = Query(
        None, description="User's Solana wallet address"
    ),
):
    """Read token balances on both chains for a Privy user.

    Returns balances in raw token units. The frontend converts
    to human-readable amounts using known decimals.
    """
    _check_read_rate_limit(_get_client_ip(request))

    if not base_address and not solana_address:
        raise HTTPException(
            400, "At least one of base_address or solana_address is required"
        )

    balances: dict = {}
    errors: list[dict] = []

    if base_address:
        if not ETH_ADDRESS_RE.match(base_address):
            raise HTTPException(400, "Invalid base_address")
        try:
            from src.chains.base.client import get_balance, get_eth_balance

            balances["base"] = {
                "usdc": str(get_balance(base_address, settings.usdc_address)),
                "weth": str(get_balance(base_address, settings.weth_address)),
                "eth": str(get_eth_balance(base_address)),
            }
        except Exception:
            logger.exception("Failed to read Base balances for %s", base_address)
            errors.append(
                {
                    "chain": "base",
                    "message": "Could not read Base balances",
                }
            )

    if solana_address:
        if not is_valid_solana_address(solana_address):
            raise HTTPException(400, "Invalid solana_address")
        try:
            from src.chains.solana.client import (
                get_balance as sol_get_balance,
                get_sol_balance,
            )

            sol_balances: dict[str, str] = {
                "sol": str(get_sol_balance(solana_address)),
            }
            if settings.solana_usdc_mint:
                sol_balances["usdc"] = str(
                    sol_get_balance(solana_address, settings.solana_usdc_mint)
                )
            if settings.solana_wsol_mint:
                sol_balances["wsol"] = str(
                    sol_get_balance(solana_address, settings.solana_wsol_mint)
                )
            if settings.solana_jup_mint:
                sol_balances["jup"] = str(
                    sol_get_balance(solana_address, settings.solana_jup_mint)
                )
            if settings.solana_xau_mint:
                sol_balances["xau"] = str(
                    sol_get_balance(solana_address, settings.solana_xau_mint)
                )
            balances["solana"] = sol_balances
        except Exception:
            logger.exception("Failed to read Solana balances for %s", solana_address)
            errors.append(
                {
                    "chain": "solana",
                    "message": "Could not read Solana balances",
                }
            )

    if not balances and errors:
        raise HTTPException(502, "Could not read balances from any chain")

    return {"balances": balances, "errors": errors}
