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

from solders.pubkey import Pubkey as SolPubkey  # type: ignore[import-untyped]
from solders.signature import Signature as SolSignature  # type: ignore[import-untyped]

from src.api.deps import require_mm_api_key
from src.chains.explorer import tx_explorer_url
from src.chains.solana.client import get_solana_maker_nonce
from src.config import settings
from src.contracts.web3_client import get_batch_settler, get_w3
from src.crypto.ed25519 import build_solana_quote_message, verify_solana_quote
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
    QuoteSubmission,
)
from src.pricing.assets import Asset, get_chain_for_asset
from src.pricing.chainlink import get_asset_price
from src.pricing.deribit import get_iv
from src.pricing.utils import get_expiries
from src.bots.otoken_manager import _parse_custom_expiries

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mm", tags=["Market Making"])


def _normalize_mm_address(addr: str) -> str:
    """Normalize MM address: lowercase for EVM (0x), as-is for Solana (base58)."""
    if addr.startswith("0x"):
        return addr.lower()
    return addr


def _resolve_nonce(
    chain: str, body: QuoteBatchRequest, mm_address: str
) -> tuple[str, int]:
    """Return (mm_id, on_chain_nonce) for the chain.

    For Solana: mm_id is the maker pubkey, nonce from MakerState PDA.
    For Base: mm_id is the lowercased EVM address, nonce from BatchSettler.
    """
    if chain == "solana":
        maker = body.quotes[0].maker
        if not maker:
            raise HTTPException(400, "maker required for Solana quotes")
        try:
            nonce = get_solana_maker_nonce(maker)
        except Exception:
            logger.exception("Failed to read Solana makerNonce for %s", maker)
            raise HTTPException(502, "Could not read Solana makerNonce")
        return maker, nonce
    else:
        try:
            settler = get_batch_settler()
            nonce = settler.functions.makerNonce(
                Web3.to_checksum_address(mm_address)
            ).call()
        except Exception:
            logger.exception("Failed to read makerNonce for %s", mm_address)
            raise HTTPException(502, "Could not read on-chain makerNonce")
        return mm_address.lower(), nonce


@router.post(
    "/quotes",
    response_model=QuoteBatchResponse,
    summary="Submit signed quotes",
)
async def submit_quotes(
    body: QuoteBatchRequest,
    mm_address: str = Depends(require_mm_api_key),
):
    """Submit a batch of signed quotes (EIP-712 for Base, ed25519 for Solana).

    The chain is determined from the first quote — MMs send per-chain batches.
    Signatures, nonces, and addresses are validated per-chain. Quotes with
    invalid signatures, expired deadlines, or wrong makerNonce are rejected.
    """
    now_ts = int(time.time())
    accepted = 0
    errors: list[str] = []

    chains_in_batch = {q.chain for q in body.quotes}
    if len(chains_in_batch) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"All quotes in a batch must target the same chain, got: {chains_in_batch}",
        )
    chain = chains_in_batch.pop()

    mm_id, on_chain_nonce = _resolve_nonce(chain, body, mm_address)

    rows_to_upsert = []

    for i, q in enumerate(body.quotes):
        label = f"quote[{i}]"

        if q.deadline <= now_ts:
            errors.append(f"{label}: deadline {q.deadline} already passed")
            continue

        if q.maker_nonce != on_chain_nonce:
            errors.append(
                f"{label}: makerNonce mismatch (got {q.maker_nonce}, "
                f"on-chain is {on_chain_nonce})"
            )
            continue

        if chain == "solana":
            valid = _verify_solana_sig(q, label, errors)
            if not valid:
                continue
            otoken_addr = q.otoken_address
        else:
            valid = _verify_base_sig(q, label, mm_address, errors)
            if not valid:
                continue
            otoken_addr = q.otoken_address.lower()

        rows_to_upsert.append(
            {
                "mm_address": mm_id,
                "otoken_address": otoken_addr,
                "bid_price": str(q.bid_price),
                "deadline": q.deadline,
                "quote_id": str(q.quote_id),
                "max_amount": str(q.max_amount),
                "maker_nonce": q.maker_nonce,
                "signature": q.signature,
                "chain": chain,
                "asset": q.asset,
                "strike_price": q.strike_price,
                "expiry": q.expiry,
                "is_put": q.is_put,
                "is_active": True,
            }
        )

    if rows_to_upsert:
        try:
            db = get_client()
            otoken_addrs = list({r["otoken_address"] for r in rows_to_upsert})
            db.table("mm_quotes").update({"is_active": False}).eq(
                "mm_address", mm_id
            ).eq("is_active", True).in_("otoken_address", otoken_addrs).execute()
            db.table("mm_quotes").upsert(
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


def _verify_solana_sig(q: QuoteSubmission, label: str, errors: list[str]) -> bool:
    """Verify an ed25519 Solana quote signature. Returns True if valid."""
    try:
        pubkey = SolPubkey.from_string(q.maker)
        otoken_bytes = bytes(SolPubkey.from_string(q.otoken_address))
        msg = build_solana_quote_message(
            otoken_bytes,
            bid_price=q.bid_price,
            deadline=q.deadline,
            quote_id=q.quote_id,
            max_amount=q.max_amount,
            maker_nonce=q.maker_nonce,
        )
        sig_bytes = bytes(SolSignature.from_string(q.signature))
        if not verify_solana_quote(pubkey, msg, sig_bytes):
            errors.append(f"{label}: signature does not match maker pubkey")
            return False
    except Exception:
        logger.exception("%s: Solana signature verification failed", label)
        errors.append(f"{label}: invalid signature")
        return False
    return True


def _verify_base_sig(
    q: QuoteSubmission, label: str, mm_address: str, errors: list[str]
) -> bool:
    """Verify an EIP-712 Base quote signature. Returns True if valid."""
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
        logger.exception("%s: EIP-712 signature recovery failed", label)
        errors.append(f"{label}: invalid signature")
        return False

    if recovered.lower() != mm_address.lower():
        logger.warning(
            "%s: signer mismatch (recovered %s, expected %s)",
            label,
            recovered,
            mm_address,
        )
        errors.append(f"{label}: signature does not match authenticated MM address")
        return False
    return True


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
            .eq("mm_address", _normalize_mm_address(mm_address))
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
            asset=row.get("asset", "eth"),
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
            .eq("mm_address", _normalize_mm_address(mm_address))
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
            .eq("mm_address", _normalize_mm_address(mm_address))
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
            chain=r.get("chain", "base"),
            tx_url=tx_explorer_url(r.get("tx_hash"), r.get("chain", "base")),
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
            .eq("mm_address", _normalize_mm_address(mm_address))
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
            .eq("mm_address", _normalize_mm_address(mm_address))
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
            .eq("mm_address", _normalize_mm_address(mm_address))
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
async def get_market(
    mm_address: str = Depends(require_mm_api_key),
    asset: Asset = Query(default=Asset.ETH, description="Underlying asset"),
):
    """Return market data for MM's pricing engine for a given asset."""
    from src.pricing.assets import get_asset_config, get_chain_for_asset
    from src.chains import Chain

    chain = get_chain_for_asset(asset)

    try:
        if chain == Chain.SOLANA:
            from src.chains.solana.oracle import get_spot_price

            spot, _ = get_spot_price(asset)
        else:
            spot, _ = get_asset_price(asset)
    except Exception:
        logger.exception("Failed to fetch %s spot price", asset.value)
        raise HTTPException(
            status_code=502, detail=f"Could not fetch {asset.value.upper()} spot"
        )

    try:
        iv = await get_iv(asset)
    except Exception:
        logger.exception("Failed to fetch %s IV from Deribit", asset.value)
        raise HTTPException(status_code=502, detail="Could not fetch IV")

    # Gas price only relevant for Base
    gas_price_gwei = 0.0
    if chain == Chain.BASE:
        try:
            w3 = get_w3()
            gas_price_wei = w3.eth.gas_price
            gas_price_gwei = gas_price_wei / 1e9
        except Exception:
            logger.exception("Failed to fetch gas price")

    cfg = get_asset_config(asset)
    # Base: hex addresses are case-insensitive → lowercase
    # Solana: base58 addresses are case-sensitive → keep as-is
    underlying_addr = (
        cfg.underlying_address
        if chain == Chain.SOLANA
        else cfg.underlying_address.lower()
    )

    otokens: list[OTokenInfo] = []
    active_expiries = _parse_custom_expiries() or get_expiries()
    try:
        client = get_client()
        result = (
            client.table("available_otokens")
            .select("otoken_address,strike_price,expiry,is_put")
            .eq("underlying", underlying_addr)
            .in_("expiry", active_expiries)
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
        asset=asset.value,
        spot=spot,
        iv=iv,
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
    asset_val = body.asset.lower()
    chain_val = get_chain_for_asset(Asset(asset_val)).value
    row = {
        "mm_address": _normalize_mm_address(mm_address),
        "asset": asset_val,
        "chain": chain_val,
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
        client.table("mm_capacity").upsert(
            row, on_conflict="mm_address,asset"
        ).execute()
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
