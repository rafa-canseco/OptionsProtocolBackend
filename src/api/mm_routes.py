"""
Market Maker quote management endpoints.

POST /mm/quotes — submit signed quotes
GET  /mm/quotes — retrieve active quotes
DELETE /mm/quotes — cancel all active quotes
"""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from web3 import Web3

from src.api.deps import require_mm_api_key
from src.contracts.web3_client import get_batch_settler
from src.crypto.eip712 import recover_quote_signer
from src.db.database import get_client
from src.models.mm import (
    QuoteBatchRequest,
    QuoteBatchResponse,
    QuoteResponse,
)

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
        raise HTTPException(status_code=502, detail="Could not read on-chain makerNonce")

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
                label, recovered, mm_address,
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
