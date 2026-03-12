"""
Market Maker endpoints.

Quote management:
  POST /mm/quotes — submit signed quotes
  GET  /mm/quotes — retrieve active quotes
  DELETE /mm/quotes — cancel all active quotes

Monitoring:
  GET /mm/fills     — filled trades
  GET /mm/positions — open positions grouped by oToken
  GET /mm/exposure  — aggregated risk summary
  GET /mm/market    — market data for pricing engine
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from web3 import Web3

from src.api.deps import require_mm_api_key
from src.config import settings
from src.contracts.web3_client import get_batch_settler, get_w3
from src.crypto.eip712 import recover_quote_signer
from src.db.database import get_client
from src.models.mm import (
    CapacityUpdateRequest,
    ExpiryBucket,
    ExposureResponse,
    FillResponse,
    MarketDataResponse,
    OTokenInfo,
    PositionGroup,
    QuoteBatchRequest,
    QuoteBatchResponse,
    QuoteResponse,
)
from src.pricing.chainlink import get_eth_price
from src.pricing.deribit import get_eth_iv

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mm", tags=["Market Making"])


@router.post(
    "/quotes",
    response_model=QuoteBatchResponse,
    summary="Submit signed quotes",
)
async def submit_quotes(
    body: QuoteBatchRequest,
    mm_address: str = Depends(require_mm_api_key),
):
    """Submit a batch of EIP-712 signed quotes.

    Each quote's signature is verified: the recovered signer must match
    the MM address associated with the API key. Quotes with invalid
    signatures, expired deadlines, or wrong makerNonce are rejected.
    """
    now_ts = int(time.time())
    accepted = 0
    errors: list[str] = []

    # Read the on-chain makerNonce for this MM
    try:
        settler = get_batch_settler()
        on_chain_nonce = settler.functions.makerNonce(
            Web3.to_checksum_address(mm_address)
        ).call()
    except Exception:
        logger.exception("Failed to read makerNonce for %s", mm_address)
        raise HTTPException(
            status_code=502, detail="Could not read on-chain makerNonce"
        )

    rows_to_upsert = []

    for i, q in enumerate(body.quotes):
        label = f"quote[{i}]"

        # Check deadline
        if q.deadline <= now_ts:
            errors.append(f"{label}: deadline {q.deadline} already passed")
            continue

        # Check makerNonce matches on-chain
        if q.maker_nonce != on_chain_nonce:
            errors.append(
                f"{label}: makerNonce mismatch (got {q.maker_nonce}, "
                f"on-chain is {on_chain_nonce})"
            )
            continue

        # Verify EIP-712 signature
        try:
            recovered = recover_quote_signer(
                otoken=q.otoken_address,
                bid_price=q.bid_price,
                deadline=q.deadline,
                quote_id=q.quote_id,
                max_amount=q.max_amount,
                maker_nonce=q.maker_nonce,
                signature=q.signature,
            )
        except Exception:
            logger.exception("%s: signature recovery failed", label)
            errors.append(f"{label}: invalid signature")
            continue

        if recovered.lower() != mm_address.lower():
            logger.warning(
                "%s: signer mismatch (recovered %s, expected %s)",
                label,
                recovered,
                mm_address,
            )
            errors.append(f"{label}: signature does not match authenticated MM address")
            continue

        rows_to_upsert.append(
            {
                "mm_address": mm_address.lower(),
                "otoken_address": q.otoken_address.lower(),
                "bid_price": str(q.bid_price),
                "deadline": q.deadline,
                "quote_id": str(q.quote_id),
                "max_amount": str(q.max_amount),
                "maker_nonce": q.maker_nonce,
                "signature": q.signature,
                "strike_price": q.strike_price,
                "expiry": q.expiry,
                "is_put": q.is_put,
                "is_active": True,
            }
        )

    if rows_to_upsert:
        try:
            client = get_client()
            client.table("mm_quotes").upsert(
                rows_to_upsert, on_conflict="mm_address,quote_id"
            ).execute()
            accepted = len(rows_to_upsert)
        except Exception:
            logger.exception("Failed to upsert mm_quotes")
            raise HTTPException(status_code=502, detail="Database write failed")

    return QuoteBatchResponse(
        accepted=accepted,
        rejected=len(body.quotes) - accepted,
        errors=errors,
    )


@router.get(
    "/quotes",
    response_model=list[QuoteResponse],
    summary="Get active quotes",
)
async def get_quotes(mm_address: str = Depends(require_mm_api_key)):
    """Retrieve all active, non-expired quotes for the authenticated MM."""
    now_ts = int(time.time())
    try:
        client = get_client()
        result = (
            client.table("mm_quotes")
            .select("*")
            .eq("mm_address", mm_address.lower())
            .eq("is_active", True)
            .gt("deadline", now_ts)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch quotes for %s", mm_address)
        raise HTTPException(status_code=502, detail="Could not fetch quotes")

    return [
        QuoteResponse(
            id=row["id"],
            otoken_address=row["otoken_address"],
            bid_price=str(row["bid_price"]),
            deadline=row["deadline"],
            quote_id=str(row["quote_id"]),
            max_amount=str(row["max_amount"]),
            maker_nonce=row["maker_nonce"],
            signature=row["signature"],
            strike_price=row.get("strike_price"),
            expiry=row.get("expiry"),
            is_put=row.get("is_put"),
            is_active=row["is_active"],
            created_at=str(row["created_at"]),
        )
        for row in (result.data or [])
    ]


@router.delete(
    "/quotes",
    summary="Cancel all active quotes",
)
async def cancel_quotes(mm_address: str = Depends(require_mm_api_key)):
    """Set is_active=false for all quotes belonging to this MM.

    This immediately stops the backend from serving these quotes in GET /prices.
    On-chain, the quotes remain valid until the MM calls incrementMakerNonce().
    """
    try:
        client = get_client()
        result = (
            client.table("mm_quotes")
            .update({"is_active": False})
            .eq("mm_address", mm_address.lower())
            .eq("is_active", True)
            .execute()
        )
        cancelled = len(result.data) if result.data else 0
    except Exception:
        logger.exception("Failed to cancel quotes for %s", mm_address)
        raise HTTPException(status_code=502, detail="Could not cancel quotes")

    return {"cancelled": cancelled}


@router.get(
    "/fills",
    response_model=list[FillResponse],
    summary="Get filled trades",
    tags=["MM Monitoring"],
)
async def get_fills(
    mm_address: str = Depends(require_mm_api_key),
    since: int | None = Query(default=None, description="Unix ts filter"),
    otoken: str | None = Query(default=None, description="oToken address filter"),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Return trades executed against the MM's quotes."""
    try:
        client = get_client()
        q = (
            client.table("order_events")
            .select("*")
            .eq("mm_address", mm_address.lower())
        )
        if since is not None:
            q = q.gte("indexed_at", _ts_to_iso(since))
        if otoken is not None:
            q = q.eq("otoken_address", otoken.lower())
        result = q.order("indexed_at", desc=True).limit(limit).execute()
    except Exception:
        logger.exception("Failed to fetch fills for %s", mm_address)
        raise HTTPException(status_code=502, detail="Could not fetch fills")

    return [
        FillResponse(
            tx_hash=r["tx_hash"],
            block_number=r["block_number"],
            otoken_address=r["otoken_address"],
            amount=str(r["amount"]),
            gross_premium=str(r.get("gross_premium", r["premium"])),
            net_premium=str(r.get("net_premium", "")),
            protocol_fee=str(r.get("protocol_fee", "")),
            collateral=str(r["collateral"]),
            user_address=r["user_address"],
            vault_id=r["vault_id"],
            strike_price=_safe_float(r.get("strike_price")),
            expiry=r.get("expiry"),
            is_put=r.get("is_put"),
            indexed_at=str(r["indexed_at"]),
        )
        for r in (result.data or [])
    ]


@router.get(
    "/positions",
    response_model=list[PositionGroup],
    summary="Get open positions",
    tags=["MM Monitoring"],
)
async def get_positions(mm_address: str = Depends(require_mm_api_key)):
    """Return open positions grouped by oToken (not yet expired)."""
    now_ts = int(time.time())
    try:
        client = get_client()
        result = (
            client.table("order_events")
            .select("*")
            .eq("mm_address", mm_address.lower())
            .gt("expiry", now_ts)
            .order("expiry")
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch positions for %s", mm_address)
        raise HTTPException(status_code=502, detail="Could not fetch positions")

    groups: dict[str, dict] = {}
    for r in result.data or []:
        key = r["otoken_address"]
        if key not in groups:
            groups[key] = {
                "otoken_address": key,
                "strike_price": float(r.get("strike_price") or 0),
                "expiry": r.get("expiry") or 0,
                "is_put": r.get("is_put", False),
                "total_amount": Decimal("0"),
                "total_premium_earned": Decimal("0"),
                "fill_count": 0,
            }
        g = groups[key]
        g["total_amount"] += Decimal(str(r["amount"]))
        g["total_premium_earned"] += Decimal(str(r.get("gross_premium", r["premium"])))
        g["fill_count"] += 1

    return [
        PositionGroup(
            otoken_address=g["otoken_address"],
            strike_price=g["strike_price"],
            expiry=g["expiry"],
            is_put=g["is_put"],
            total_amount=str(g["total_amount"]),
            total_premium_earned=str(g["total_premium_earned"]),
            fill_count=g["fill_count"],
        )
        for g in groups.values()
    ]


@router.get(
    "/exposure",
    response_model=ExposureResponse,
    summary="Get risk exposure",
    tags=["MM Monitoring"],
)
async def get_exposure(mm_address: str = Depends(require_mm_api_key)):
    """Return aggregated risk summary for the MM."""
    now_ts = int(time.time())
    client = get_client()

    try:
        # Active quotes
        quotes_result = (
            client.table("mm_quotes")
            .select("max_amount")
            .eq("mm_address", mm_address.lower())
            .eq("is_active", True)
            .gt("deadline", now_ts)
            .execute()
        )
        quotes = quotes_result.data or []
        active_count = len(quotes)
        active_notional = sum(Decimal(str(q["max_amount"])) for q in quotes)

        # All fills for this MM
        fills_result = (
            client.table("order_events")
            .select("expiry,amount,gross_premium,premium,is_settled")
            .eq("mm_address", mm_address.lower())
            .execute()
        )
        fills = fills_result.data or []
    except Exception:
        logger.exception("Failed to fetch exposure for %s", mm_address)
        raise HTTPException(status_code=502, detail="Could not fetch exposure")

    # Group open positions by expiry
    expiry_buckets: dict[int, dict] = defaultdict(
        lambda: {"count": 0, "amount": Decimal("0")}
    )
    total_premium = Decimal("0")
    pending_settlement = 0

    for f in fills:
        prem = f.get("gross_premium") or f.get("premium", "0")
        total_premium += Decimal(str(prem))

        expiry = f.get("expiry")
        if expiry and expiry > now_ts:
            bucket = expiry_buckets[expiry]
            bucket["count"] += 1
            bucket["amount"] += Decimal(str(f["amount"]))

        # Positions past expiry but not yet settled
        if expiry and expiry <= now_ts and not f.get("is_settled"):
            pending_settlement += 1

    return ExposureResponse(
        active_quotes_count=active_count,
        active_quotes_notional=str(active_notional),
        open_positions_by_expiry=[
            ExpiryBucket(
                expiry=exp,
                position_count=b["count"],
                total_amount=str(b["amount"]),
            )
            for exp, b in sorted(expiry_buckets.items())
        ],
        total_premium_earned=str(total_premium),
        pending_settlement_count=pending_settlement,
    )


@router.get(
    "/market",
    response_model=MarketDataResponse,
    summary="Get market data",
    tags=["MM Monitoring"],
)
async def get_market(mm_address: str = Depends(require_mm_api_key)):
    """Return market data for MM's pricing engine."""
    try:
        eth_spot, _ = get_eth_price()
    except Exception:
        logger.exception("Failed to fetch ETH spot price")
        raise HTTPException(status_code=502, detail="Could not fetch ETH spot")

    try:
        iv = await get_eth_iv()
    except Exception:
        logger.exception("Failed to fetch ETH IV from Deribit")
        raise HTTPException(status_code=502, detail="Could not fetch IV")

    try:
        w3 = get_w3()
        gas_price_wei = w3.eth.gas_price
        gas_price_gwei = gas_price_wei / 1e9
    except Exception:
        logger.exception("Failed to fetch gas price")
        gas_price_gwei = 0.0

    # Fetch available oTokens created by the otoken_manager
    otokens: list[OTokenInfo] = []
    now_ts = int(time.time())
    try:
        client = get_client()
        result = (
            client.table("available_otokens")
            .select("otoken_address,strike_price,expiry,is_put")
            .gt("expiry", now_ts)
            .execute()
        )
        for r in result.data or []:
            otokens.append(
                OTokenInfo(
                    address=r["otoken_address"],
                    strike_price=float(r["strike_price"]),
                    expiry=r["expiry"],
                    is_put=r["is_put"],
                )
            )
    except Exception:
        logger.exception("Failed to fetch available oTokens")
        raise HTTPException(status_code=502, detail="Could not fetch available oTokens")

    return MarketDataResponse(
        eth_spot=eth_spot,
        eth_iv=iv,
        protocol_fee_bps=settings.protocol_fee_bps,
        gas_price_gwei=round(gas_price_gwei, 4),
        available_otokens=otokens,
    )


@router.post(
    "/capacity",
    summary="Report MM capacity",
    tags=["MM Monitoring"],
)
async def report_capacity(
    body: CapacityUpdateRequest,
    mm_address: str = Depends(require_mm_api_key),
):
    """Receive a capacity report from a market maker.

    The mm_address is taken from the authenticated API key, not the body.
    Upserts into mm_capacity keyed by mm_address.
    """
    row = {
        "mm_address": mm_address.lower(),
        "asset": body.asset,
        "capacity_eth": body.capacity_eth,
        "capacity_usd": body.capacity_usd,
        "status": body.status,
        "reported_at": datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(),
    }
    for field in (
        "premium_pool_usd",
        "hedge_pool_usd",
        "hedge_pool_withdrawable_usd",
        "leverage",
        "open_positions_count",
        "open_positions_notional_usd",
    ):
        val = getattr(body, field)
        if val is not None:
            row[field] = val

    try:
        client = get_client()
        client.table("mm_capacity").upsert(row, on_conflict="mm_address").execute()
    except Exception:
        logger.exception("Failed to upsert mm_capacity for %s", mm_address)
        raise HTTPException(status_code=502, detail="Could not save capacity")

    return {"status": "ok"}


def _ts_to_iso(ts: int) -> str:
    """Convert unix timestamp to ISO 8601 string for Supabase gte filter."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
