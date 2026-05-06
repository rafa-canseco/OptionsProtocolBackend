"""Bridge Relayer API — POST /api/bridge-and-trade, GET /api/bridge-status."""

import logging
import re

from fastapi import APIRouter, HTTPException

from src.bridge.cctp import (
    build_solana_cctp_burn_transaction,
    get_domain_for_chain,
    submit_solana_cctp_burn_transaction,
)
from src.bridge.models import (
    BridgeAndTradeRequest,
    BridgeChain,
    BridgeJobReserveRequest,
    BridgeJobState,
    BridgeJobStatus,
    SolanaCCTPBurnPrepareRequest,
    SolanaCCTPBurnPrepareResponse,
    SolanaCCTPBurnSubmitRequest,
)
from src.bridge.relayer import enqueue_job
from src.chains import Chain
from src.chains.address import ETH_ADDRESS_RE, is_valid_solana_address
from src.config import is_chain_tradable, settings
from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Bridge"])

EVM_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
# Solana signatures are base58, 87-88 chars
SOLANA_SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{87,88}$")
PENDING_BURN_PREFIX = "pending:"


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


def _validate_bridge_chains(source_chain: str, dest_chain: str) -> None:
    if source_chain == dest_chain:
        raise HTTPException(400, "source_chain and dest_chain must differ")
    for chain in (source_chain, dest_chain):
        if not is_chain_tradable(chain):
            raise HTTPException(
                403,
                f"Trading is disabled for {chain} in {settings.app_env}",
            )


def _create_bridge_job_or_raise(body: BridgeAndTradeRequest) -> str:
    client = get_client()

    # Dedup by burn_tx_hash
    try:
        existing = (
            client.table("bridge_jobs")
            .select("id, status")
            .eq("burn_tx_hash", body.burn_tx_hash)
            .execute()
        )
    except Exception:
        logger.exception("Failed to check for duplicate burn tx")
        raise HTTPException(502, "Could not check for duplicate bridge job")
    if existing.data:
        raise HTTPException(
            409,
            f"Bridge job already exists for burn tx "
            f"{body.burn_tx_hash[:16]}... "
            f"(job {existing.data[0]['id']})",
        )

    # Dedup by quote_id
    if body.quote_id:
        try:
            existing_quote = (
                client.table("bridge_jobs")
                .select("*")
                .eq("quote_id", body.quote_id)
                .execute()
            )
        except Exception:
            logger.exception("Failed to check for duplicate quote")
            raise HTTPException(502, "Could not check for duplicate quote")
        if existing_quote.data:
            reserved = existing_quote.data[0]
            if (
                reserved.get("source_chain") == body.source_chain.value
                and reserved.get("dest_chain") == body.dest_chain.value
                and reserved.get("user_id") == body.user_id
                and reserved.get("status") == BridgeJobState.PENDING.value
                and str(reserved.get("burn_tx_hash", "")).startswith(
                    PENDING_BURN_PREFIX
                )
            ):
                return _finalize_bridge_reservation_or_raise(reserved, body)
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
    return job_id


def _finalize_bridge_reservation_or_raise(
    reserved: dict, body: BridgeAndTradeRequest
) -> str:
    client = get_client()
    fields = {
        "burn_tx_hash": body.burn_tx_hash,
        "burn_amount": body.burn_amount,
        "mint_recipient": body.mint_recipient,
        "signed_trade_tx": body.signed_trade_tx,
        "error_message": None,
    }
    try:
        result = (
            client.table("bridge_jobs")
            .update(fields)
            .eq("id", reserved["id"])
            .execute()
        )
    except Exception:
        logger.exception("Failed to finalize bridge reservation %s", reserved["id"])
        raise HTTPException(502, "Could not finalize bridge job reservation")

    if not result.data:
        raise HTTPException(502, "Bridge job reservation finalize returned no data")

    job_id = result.data[0]["id"]
    enqueue_job(job_id)
    return job_id


def _create_bridge_reservation_or_raise(body: BridgeJobReserveRequest) -> str:
    if not body.quote_id:
        raise HTTPException(400, "quote_id is required")
    if body.source_chain != BridgeChain.BASE or body.dest_chain != BridgeChain.SOLANA:
        raise HTTPException(
            400,
            "Bridge reservation currently supports source_chain=base and dest_chain=solana",
        )
    if not is_valid_solana_address(body.mint_recipient):
        raise HTTPException(400, "mint_recipient must be a Solana address")
    _validate_solana_cctp_mint_recipient_or_raise(body.mint_recipient)

    client = get_client()
    try:
        existing_quote = (
            client.table("bridge_jobs")
            .select("id, status")
            .eq("quote_id", body.quote_id)
            .execute()
        )
    except Exception:
        logger.exception("Failed to check for duplicate quote reservation")
        raise HTTPException(502, "Could not check for duplicate quote")
    if existing_quote.data:
        raise HTTPException(
            409,
            f"Bridge job already exists for quote "
            f"{body.quote_id} "
            f"(job {existing_quote.data[0]['id']})",
        )

    row = {
        "user_id": body.user_id,
        "source_chain": body.source_chain.value,
        "dest_chain": body.dest_chain.value,
        "status": BridgeJobState.PENDING.value,
        "burn_tx_hash": f"{PENDING_BURN_PREFIX}{body.quote_id}",
        "burn_amount": body.burn_amount,
        "mint_recipient": body.mint_recipient,
        "quote_id": body.quote_id,
        "signed_trade_tx": body.signed_trade_tx,
    }

    try:
        result = client.table("bridge_jobs").insert(row).execute()
    except Exception:
        logger.exception("Failed to reserve bridge job for quote %s", body.quote_id)
        raise HTTPException(
            409,
            f"Bridge job already exists or could not be reserved for quote {body.quote_id}",
        )

    if not result.data:
        raise HTTPException(502, "Bridge job reservation returned no data")

    return result.data[0]["id"]


def _validate_solana_cctp_mint_recipient_or_raise(address: str) -> None:
    """Validate Base→Solana CCTP recipient is a USDC token account.

    Circle's Solana TokenMessenger receives into a token account, not the
    owner's wallet address. Accepting an owner wallet strands the message:
    Circle attests it, but Solana receive_message rejects it because the
    recipient account is not owned by the SPL Token program.
    """
    from solders.pubkey import Pubkey

    from src.chains.solana.client import get_solana_client

    try:
        account = get_solana_client().get_account_info(Pubkey.from_string(address)).value
    except Exception:
        logger.exception("Failed to validate Solana CCTP mint recipient %s", address)
        raise HTTPException(502, "Could not validate Solana mint recipient")

    if account is None:
        raise HTTPException(
            400,
            "mint_recipient must be an existing Solana USDC token account",
        )

    token_program = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    if str(account.owner) != token_program:
        raise HTTPException(
            400,
            "mint_recipient must be a Solana USDC token account, not wallet owner",
        )

    data = bytes(account.data)
    if len(data) < 64:
        raise HTTPException(400, "mint_recipient is not a valid token account")
    mint = str(Pubkey.from_bytes(data[:32]))
    if mint != settings.cctp_solana_usdc_mint:
        raise HTTPException(400, "mint_recipient token account must be USDC")


def _ensure_quote_unused_or_raise(quote_id: str | None) -> None:
    if not quote_id:
        return

    client = get_client()
    try:
        existing_quote = (
            client.table("bridge_jobs")
            .select("id, status")
            .eq("quote_id", quote_id)
            .execute()
        )
    except Exception:
        logger.exception("Failed to check for duplicate quote before burn")
        raise HTTPException(502, "Could not check for duplicate quote")
    if existing_quote.data:
        raise HTTPException(
            409,
            f"Bridge job already exists for quote "
            f"{quote_id} "
            f"(job {existing_quote.data[0]['id']})",
        )


def _create_solana_burn_reservation_or_raise(body: SolanaCCTPBurnSubmitRequest) -> str:
    """Reserve a Solana burn intent before broadcasting the signed tx.

    The Solana burn is irreversible once broadcast. Reserving by quote_id first
    prevents frontend/network retries from submitting multiple signed burn txs
    for the same trade intent.
    """
    if not body.quote_id:
        raise HTTPException(
            400,
            "quote_id is required for Solana CCTP burn submit idempotency",
        )

    active_statuses = [
        BridgeJobState.PENDING.value,
        BridgeJobState.ATTESTING.value,
        BridgeJobState.MINTING.value,
        BridgeJobState.TRADING.value,
    ]
    client = get_client()
    try:
        active = (
            client.table("bridge_jobs")
            .select("id, status, quote_id")
            .eq("user_id", body.user_id)
            .eq("source_chain", BridgeChain.SOLANA.value)
            .in_("status", active_statuses)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "Failed to check active Solana CCTP bridge for user %s",
            body.user_id,
        )
        raise HTTPException(502, "Could not check active bridge jobs")

    if active.data:
        existing = active.data[0]
        raise HTTPException(
            409,
            f"Active Solana bridge job already exists "
            f"(job {existing['id']}, status {existing['status']})",
        )

    row = {
        "user_id": body.user_id,
        "source_chain": BridgeChain.SOLANA.value,
        "dest_chain": body.dest_chain.value,
        "status": BridgeJobState.PENDING,
        "burn_tx_hash": f"pending:{body.quote_id}",
        "burn_amount": body.burn_amount,
        "mint_recipient": body.mint_recipient,
        "quote_id": body.quote_id,
        "signed_trade_tx": body.signed_trade_tx,
    }

    try:
        result = client.table("bridge_jobs").insert(row).execute()
    except Exception:
        logger.exception("Failed to reserve Solana CCTP burn for quote %s", body.quote_id)
        raise HTTPException(
            409,
            f"Bridge job already exists or could not be reserved for quote {body.quote_id}",
        )

    if not result.data:
        raise HTTPException(502, "Bridge job reservation returned no data")

    return result.data[0]["id"]


def _mark_bridge_job_failed(job_id: str, error_message: str) -> None:
    client = get_client()
    try:
        (
            client.table("bridge_jobs")
            .update(
                {
                    "status": BridgeJobState.FAILED.value,
                    "error_message": error_message[:500],
                }
            )
            .eq("id", job_id)
            .execute()
        )
    except Exception:
        logger.exception("Failed to mark bridge job %s failed", job_id)


def _finalize_solana_burn_reservation_or_raise(job_id: str, burn_tx_hash: str) -> None:
    client = get_client()
    try:
        result = (
            client.table("bridge_jobs")
            .update({"burn_tx_hash": burn_tx_hash, "status": BridgeJobState.PENDING.value})
            .eq("id", job_id)
            .execute()
        )
    except Exception:
        logger.exception("Failed to finalize Solana CCTP burn job %s", job_id)
        raise HTTPException(502, "Could not finalize bridge job")

    if not result.data:
        raise HTTPException(502, "Bridge job finalize returned no data")


@router.post(
    "/bridge-and-trade/reserve",
    summary="Reserve a Base to Solana bridge job before burn",
)
async def reserve_bridge_and_trade(body: BridgeJobReserveRequest):
    """Reserve quote_id before frontend broadcasts Base CCTP burn.

    This is intentionally scoped to Base → Solana. It prevents the frontend
    from burning USDC and then discovering that quote_id is already blocked.
    """
    _validate_bridge_chains(body.source_chain.value, body.dest_chain.value)
    job_id = _create_bridge_reservation_or_raise(body)
    return {"job_id": job_id, "status": "reserved"}


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
    _validate_bridge_chains(body.source_chain.value, body.dest_chain.value)
    _validate_tx_hash(body.burn_tx_hash, body.source_chain.value)

    job_id = _create_bridge_job_or_raise(body)

    return {"job_id": job_id, "status": "pending"}


@router.post(
    "/bridge/solana-cctp-burn/prepare",
    response_model=SolanaCCTPBurnPrepareResponse,
    summary="Prepare a sponsored Solana CCTP burn transaction",
)
async def prepare_solana_cctp_burn(body: SolanaCCTPBurnPrepareRequest):
    """Build a Solana CCTP burn tx that backend pays and partially signs.

    The frontend must add the user's owner signature without Privy sponsorship,
    then submit the fully signed transaction to the companion submit endpoint.
    """
    if body.dest_chain.value != "base":
        raise HTTPException(
            400,
            "Solana CCTP burn prepare currently supports dest_chain=base",
        )
    _validate_bridge_chains("solana", body.dest_chain.value)
    if not is_valid_solana_address(body.owner):
        raise HTTPException(400, "Invalid Solana owner address")
    if not ETH_ADDRESS_RE.match(body.mint_recipient):
        raise HTTPException(400, "mint_recipient must be a Base/EVM address")
    if body.destination_caller and not ETH_ADDRESS_RE.match(body.destination_caller):
        raise HTTPException(400, "destination_caller must be a Base/EVM address")

    try:
        amount = int(body.burn_amount)
        max_fee = int(body.max_fee)
    except ValueError:
        raise HTTPException(400, "burn_amount and max_fee must be integer strings")

    dest_domain = get_domain_for_chain(Chain(body.dest_chain.value))
    try:
        prepared = build_solana_cctp_burn_transaction(
            owner=body.owner,
            destination_domain=dest_domain,
            mint_recipient=body.mint_recipient,
            amount=amount,
            max_fee=max_fee,
            min_finality_threshold=body.min_finality_threshold,
            destination_caller=body.destination_caller,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        logger.exception("Failed to prepare Solana CCTP burn")
        raise HTTPException(502, "Could not prepare Solana CCTP burn")

    return {
        **prepared,
        "source_chain": "solana",
        "dest_chain": body.dest_chain.value,
        "source_domain": get_domain_for_chain(Chain.SOLANA),
        "destination_domain": dest_domain,
        "burn_amount": str(amount),
        "max_fee": str(max_fee),
        "min_finality_threshold": body.min_finality_threshold,
    }


@router.post(
    "/bridge/solana-cctp-burn/submit",
    summary="Submit a sponsored Solana CCTP burn transaction",
)
async def submit_solana_cctp_burn(body: SolanaCCTPBurnSubmitRequest):
    """Broadcast a user-signed prepared Solana burn and enqueue bridge relaying."""
    if body.dest_chain.value != "base":
        raise HTTPException(
            400,
            "Solana CCTP burn submit currently supports dest_chain=base",
        )
    _validate_bridge_chains("solana", body.dest_chain.value)
    if not ETH_ADDRESS_RE.match(body.mint_recipient):
        raise HTTPException(400, "mint_recipient must be a Base/EVM address")

    job_id = _create_solana_burn_reservation_or_raise(body)

    try:
        burn_tx_hash = submit_solana_cctp_burn_transaction(
            body.signed_transaction_base64
        )
    except ValueError as exc:
        _mark_bridge_job_failed(job_id, str(exc))
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Failed to submit Solana CCTP burn")
        _mark_bridge_job_failed(job_id, f"{type(exc).__name__}: {exc}")
        raise HTTPException(502, "Could not submit Solana CCTP burn")

    _finalize_solana_burn_reservation_or_raise(job_id, burn_tx_hash)
    enqueue_job(job_id)

    return {"burn_tx_hash": burn_tx_hash, "job_id": job_id, "status": "pending"}


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
