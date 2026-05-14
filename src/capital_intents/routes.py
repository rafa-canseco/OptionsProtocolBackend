"""Capital movement intent API for MetaVault USDC lifecycle tracking."""

import logging

from fastapi import APIRouter, HTTPException, Query

from src.bridge.models import BridgeJobState
from src.bridge.relayer import enqueue_job
from src.capital_intents.models import (
    CapitalChain,
    CapitalIntentCreate,
    CapitalIntentCreateResponse,
    CapitalIntentPatch,
    CapitalIntentResponse,
    CapitalIntentStatus,
    CapitalIntentType,
    MovementReason,
    UXStatus,
)
from src.capital_intents.sync import bridge_status_to_intent_status
from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/capital-intents", tags=["Capital Intents"])


def _default_reason(intent_type: CapitalIntentType) -> MovementReason:
    if intent_type == CapitalIntentType.DEPOSIT:
        return MovementReason.USER_DEPOSIT
    if intent_type == CapitalIntentType.RETURN:
        return MovementReason.RETURN_TO_IDLE
    return MovementReason.INITIAL_DEPLOYMENT


def _default_status(intent_type: CapitalIntentType) -> CapitalIntentStatus:
    if intent_type == CapitalIntentType.DEPOSIT:
        return CapitalIntentStatus.DEPOSIT_RECEIVED
    if intent_type == CapitalIntentType.RETURN:
        return CapitalIntentStatus.RETURNING_TO_ARC
    return CapitalIntentStatus.DEPLOYMENT_IN_FLIGHT


def _ux_status(row: dict) -> str:
    status = row["status"]
    intent_type = row["intent_type"]
    destination_chain = row["destination_chain"]

    if status == CapitalIntentStatus.DEPOSIT_RECEIVED.value:
        return UXStatus.DEPOSIT_RECEIVED.value
    if status == CapitalIntentStatus.BRIDGING.value:
        return UXStatus.BRIDGING.value
    if status == CapitalIntentStatus.WAITING_TO_BE_DEPLOYED.value:
        return UXStatus.WAITING_TO_BE_DEPLOYED.value
    if status == CapitalIntentStatus.DEPLOYMENT_IN_FLIGHT.value:
        return UXStatus.DEPLOYMENT_IN_FLIGHT.value
    if status == CapitalIntentStatus.DEPLOYED.value:
        if destination_chain == CapitalChain.SOLANA.value:
            return UXStatus.DEPLOYED_ON_SOLANA.value
        return UXStatus.DEPLOYED_ON_BASE.value
    if status == CapitalIntentStatus.PREMIUM_EARNED.value:
        return UXStatus.PREMIUM_EARNED.value
    if status == CapitalIntentStatus.ASSIGNED_ROTATING.value:
        return UXStatus.ASSIGNED_ROTATING.value
    if status == CapitalIntentStatus.RETURNING_TO_ARC.value:
        return UXStatus.RETURNING_TO_ARC.value
    if status == CapitalIntentStatus.RETURNED_TO_USDC.value:
        return UXStatus.RETURNED_TO_USDC.value
    if status in {
        CapitalIntentStatus.CLAIMABLE.value,
        CapitalIntentStatus.COMPLETED.value,
    }:
        return UXStatus.CLAIMABLE_REDEPLOYABLE.value
    if intent_type == CapitalIntentType.DEPOSIT.value:
        return UXStatus.DEPOSIT_RECEIVED.value
    return UXStatus.WAITING_TO_BE_DEPLOYED.value


def _to_response(row: dict) -> CapitalIntentResponse:
    return CapitalIntentResponse(**{**row, "ux_status": _ux_status(row)})


def _read_existing_by_idempotency(idempotency_key: str) -> dict | None:
    client = get_client()
    try:
        result = (
            client.table("capital_movement_intents")
            .select("*")
            .eq("idempotency_key", idempotency_key)
            .execute()
        )
    except Exception as exc:
        logger.exception("Failed to check capital intent idempotency")
        raise HTTPException(502, "Could not check capital intent idempotency") from exc
    return result.data[0] if result.data else None


def _create_bridge_job(body: CapitalIntentCreate) -> str:
    is_base_to_arc_deposit = (
        body.intent_type == CapitalIntentType.DEPOSIT
        and body.source_chain == CapitalChain.BASE
        and body.destination_chain == CapitalChain.ARC
    )
    is_base_solana_move = (
        body.source_chain in {CapitalChain.BASE, CapitalChain.SOLANA}
        and body.destination_chain in {CapitalChain.BASE, CapitalChain.SOLANA}
    )
    if not (is_base_solana_move or is_base_to_arc_deposit):
        raise HTTPException(
            400,
            "Bridge job creation supports base<->solana moves and base->arc deposits",
        )

    row = {
        "user_id": body.user_id,
        "source_chain": body.source_chain.value,
        "dest_chain": body.destination_chain.value,
        "status": BridgeJobState.PENDING.value,
        "burn_tx_hash": body.source_tx,
        "burn_amount": body.amount_usdc,
        "mint_recipient": body.destination_account,
        "quote_id": body.quote_id or body.idempotency_key,
        "signed_trade_tx": body.signed_trade_tx,
        "gross_amount_usdc": body.amount_usdc,
        "receiver": body.receiver,
    }

    client = get_client()
    try:
        result = client.table("bridge_jobs").insert(row).execute()
    except Exception as exc:
        logger.exception("Failed to create bridge job for capital intent")
        raise HTTPException(
            409,
            "Bridge job already exists or could not be created for this intent",
        ) from exc

    if not result.data:
        raise HTTPException(502, "Bridge job insert returned no data")

    job_id = result.data[0]["id"]
    enqueue_job(job_id)
    return job_id


def _insert_intent(
    body: CapitalIntentCreate,
    bridge_job_id: str | None,
    status_override: CapitalIntentStatus | None = None,
) -> dict:
    reason = body.movement_reason or _default_reason(body.intent_type)
    status = status_override or body.status or _default_status(body.intent_type)
    row = {
        "intent_type": body.intent_type.value,
        "movement_reason": reason.value,
        "bucket_id": body.bucket_id,
        "receiver": body.receiver,
        "source_chain": body.source_chain.value,
        "source_account": body.source_account,
        "source_tx": body.source_tx,
        "destination_chain": body.destination_chain.value,
        "destination_account": body.destination_account,
        "destination_tx": body.destination_tx,
        "amount_usdc": body.amount_usdc,
        "onchain_intent_id": body.onchain_intent_id,
        "status": status.value,
        "bridge_job_id": bridge_job_id or body.bridge_job_id,
        "idempotency_key": body.idempotency_key,
    }

    client = get_client()
    try:
        result = client.table("capital_movement_intents").insert(row).execute()
    except Exception as exc:
        logger.exception("Failed to create capital movement intent")
        raise HTTPException(502, "Could not create capital movement intent") from exc
    if not result.data:
        raise HTTPException(502, "Capital intent insert returned no data")
    return result.data[0]


def _update_intent_fields(intent_id: str, fields: dict) -> dict:
    client = get_client()
    try:
        result = (
            client.table("capital_movement_intents")
            .update(fields)
            .eq("id", intent_id)
            .execute()
        )
    except Exception as exc:
        logger.exception("Failed to update capital intent %s", intent_id)
        raise HTTPException(502, "Could not update capital movement intent") from exc
    if not result.data:
        raise HTTPException(502, "Capital intent update returned no data")
    return result.data[0]


def _load_bridge_status(bridge_job_id: str) -> dict | None:
    client = get_client()
    try:
        result = (
            client.table("bridge_jobs")
            .select("id, status, mint_tx_hash, trade_tx_hash, error_message")
            .eq("id", bridge_job_id)
            .execute()
        )
    except Exception as exc:
        logger.exception("Failed to read bridge job %s", bridge_job_id)
        raise HTTPException(502, "Could not read linked bridge job") from exc
    return result.data[0] if result.data else None


def _sync_row_from_bridge(row: dict) -> dict:
    bridge_job_id = row.get("bridge_job_id")
    if not bridge_job_id:
        return row

    bridge = _load_bridge_status(bridge_job_id)
    if not bridge:
        return row

    next_status = bridge_status_to_intent_status(
        bridge["status"],
        row["intent_type"],
    ).value
    updates: dict[str, str | None] = {}
    if row["status"] != next_status:
        updates["status"] = next_status
    if bridge.get("mint_tx_hash") and not row.get("destination_tx"):
        updates["destination_tx"] = bridge["mint_tx_hash"]
    if bridge.get("error_message") and not row.get("failure_reason"):
        updates["failure_reason"] = bridge["error_message"]

    if not updates:
        return row

    client = get_client()
    try:
        result = (
            client.table("capital_movement_intents")
            .update(updates)
            .eq("id", row["id"])
            .execute()
        )
    except Exception as exc:
        logger.exception("Failed to sync capital intent %s from bridge", row["id"])
        raise HTTPException(502, "Could not sync capital intent status") from exc
    return result.data[0] if result.data else {**row, **updates}


@router.post("", response_model=CapitalIntentCreateResponse)
async def create_capital_intent(body: CapitalIntentCreate):
    """Create a MetaVault capital movement intent.

    Base/Solana venue-to-venue moves can set create_bridge_job=true to reuse the
    existing CCTP bridge relayer while preserving a business-level intent record.
    """
    if body.idempotency_key:
        existing = _read_existing_by_idempotency(body.idempotency_key)
        if existing:
            return CapitalIntentCreateResponse(
                intent=_to_response(existing),
                bridge_job_created=False,
            )

    row = _insert_intent(
        body,
        bridge_job_id=None,
        status_override=(
            CapitalIntentStatus.PENDING if body.create_bridge_job else None
        ),
    )
    bridge_job_id = None
    if body.create_bridge_job:
        bridge_job_id = _create_bridge_job(body)
        row = _update_intent_fields(
            row["id"],
            {
                "bridge_job_id": bridge_job_id,
                "status": CapitalIntentStatus.BRIDGING.value,
            },
        )
    return CapitalIntentCreateResponse(
        intent=_to_response(row),
        bridge_job_created=bridge_job_id is not None,
    )


@router.get("/{intent_id}", response_model=CapitalIntentResponse)
async def get_capital_intent(intent_id: str):
    client = get_client()
    try:
        result = (
            client.table("capital_movement_intents")
            .select("*")
            .eq("id", intent_id)
            .execute()
        )
    except Exception as exc:
        logger.exception("Failed to read capital intent %s", intent_id)
        raise HTTPException(502, "Could not read capital intent") from exc

    if not result.data:
        raise HTTPException(404, "Capital intent not found")

    return _to_response(_sync_row_from_bridge(result.data[0]))


@router.get("", response_model=list[CapitalIntentResponse])
async def list_capital_intents(
    bucket_id: str | None = None,
    status: CapitalIntentStatus | None = None,
    limit: int = Query(50, ge=1, le=250),
):
    client = get_client()
    query = client.table("capital_movement_intents").select("*")
    if bucket_id:
        query = query.eq("bucket_id", bucket_id)
    if status:
        query = query.eq("status", status.value)
    try:
        result = query.order("created_at", desc=True).limit(limit).execute()
    except Exception as exc:
        logger.exception("Failed to list capital intents")
        raise HTTPException(502, "Could not list capital intents") from exc
    return [_to_response(row) for row in result.data]


@router.patch("/{intent_id}", response_model=CapitalIntentResponse)
async def update_capital_intent(intent_id: str, body: CapitalIntentPatch):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "No fields to update")
    for key, value in list(fields.items()):
        if hasattr(value, "value"):
            fields[key] = value.value

    client = get_client()
    try:
        result = (
            client.table("capital_movement_intents")
            .update(fields)
            .eq("id", intent_id)
            .execute()
        )
    except Exception as exc:
        logger.exception("Failed to update capital intent %s", intent_id)
        raise HTTPException(502, "Could not update capital intent") from exc

    if not result.data:
        raise HTTPException(404, "Capital intent not found")
    return _to_response(result.data[0])
