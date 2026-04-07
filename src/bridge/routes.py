"""Bridge Relayer API — POST /api/bridge-and-trade, GET /api/bridge-status."""

import logging
import re

from fastapi import APIRouter, HTTPException

from src.bridge.models import (
    BridgeAndTradeRequest,
    BridgeJobState,
    BridgeJobStatus,
)
from src.bridge.relayer import enqueue_job
from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Bridge"])

EVM_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
# Solana signatures are base58, 87-88 chars
SOLANA_SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{87,88}$")


def _validate_tx_hash(tx_hash: str, chain: str) -> None:
    """Validate tx hash format for the given chain."""
    if chain == "base":
        if not EVM_TX_RE.match(tx_hash):
            raise HTTPException(
                400,
                f"Invalid EVM tx hash: {tx_hash[:20]}...",
            )
    elif chain == "solana":
        if not SOLANA_SIG_RE.match(tx_hash):
            raise HTTPException(
                400,
                f"Invalid Solana signature: {tx_hash[:20]}...",
            )


@router.post(
    "/bridge-and-trade",
    summary="Create a bridge + trade job",
)
async def bridge_and_trade(body: BridgeAndTradeRequest):
    """Initiate a CCTP V2 bridge and optional trade execution.

    The frontend signs the burn tx and (optionally) the trade tx
    via Privy. This endpoint orchestrates: attestation polling,
    receiveMessage (mints USDC), and trade tx submission.
    """
    if body.source_chain == body.dest_chain:
        raise HTTPException(400, "source_chain and dest_chain must differ")

    _validate_tx_hash(body.burn_tx_hash, body.source_chain.value)

    client = get_client()

    # Dedup by burn_tx_hash
    existing = (
        client.table("bridge_jobs")
        .select("id, status")
        .eq("burn_tx_hash", body.burn_tx_hash)
        .execute()
    )
    if existing.data:
        raise HTTPException(
            409,
            f"Bridge job already exists for burn tx "
            f"{body.burn_tx_hash[:16]}... "
            f"(job {existing.data[0]['id']})",
        )

    # Dedup by quote_id
    if body.quote_id:
        existing_quote = (
            client.table("bridge_jobs")
            .select("id, status")
            .eq("quote_id", body.quote_id)
            .execute()
        )
        if existing_quote.data:
            raise HTTPException(
                409,
                f"Bridge job already exists for quote "
                f"{body.quote_id} "
                f"(job {existing_quote.data[0]['id']})",
            )

    # Create job
    row = {
        "user_id": body.user_id,
        "source_chain": body.source_chain.value,
        "dest_chain": body.dest_chain.value,
        "status": BridgeJobState.PENDING,
        "burn_tx_hash": body.burn_tx_hash,
        "burn_amount": body.burn_amount,
        "mint_recipient": body.mint_recipient,
        "quote_id": body.quote_id,
        "signed_trade_tx": body.signed_trade_tx,
    }

    try:
        result = client.table("bridge_jobs").insert(row).execute()
    except Exception:
        logger.exception("Failed to create bridge job")
        raise HTTPException(502, "Could not create bridge job")

    if not result.data:
        raise HTTPException(502, "Bridge job insert returned no data")

    job_id = result.data[0]["id"]
    enqueue_job(job_id)

    return {"job_id": job_id, "status": "pending"}


@router.get(
    "/bridge-status/{job_id}",
    response_model=BridgeJobStatus,
    summary="Get bridge job status",
)
async def bridge_status(job_id: str):
    """Return the current status of a bridge job."""
    client = get_client()
    try:
        result = client.table("bridge_jobs").select("*").eq("id", job_id).execute()
    except Exception:
        logger.exception("Failed to read bridge job %s", job_id)
        raise HTTPException(502, "Could not read bridge job")

    if not result.data:
        raise HTTPException(404, f"Bridge job {job_id} not found")

    return result.data[0]
