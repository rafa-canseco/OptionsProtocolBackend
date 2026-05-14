"""Synchronize business-level capital intents from bridge job state."""

import logging

from src.bridge.models import BridgeJobState
from src.capital_intents.models import CapitalIntentStatus, CapitalIntentType
from src.db.database import get_client

logger = logging.getLogger(__name__)


def bridge_status_to_intent_status(
    bridge_status: str,
    intent_type: str,
) -> CapitalIntentStatus:
    """Map technical CCTP bridge state to MetaVault capital movement state."""
    if bridge_status in {
        BridgeJobState.PENDING.value,
        BridgeJobState.ATTESTING.value,
        BridgeJobState.MINTING.value,
    }:
        return CapitalIntentStatus.BRIDGING
    if bridge_status == BridgeJobState.TRADING.value:
        return CapitalIntentStatus.DEPLOYMENT_IN_FLIGHT
    if bridge_status in {
        BridgeJobState.COMPLETED.value,
        BridgeJobState.MINT_COMPLETED.value,
    }:
        if intent_type == CapitalIntentType.DEPOSIT.value:
            return CapitalIntentStatus.WAITING_TO_BE_DEPLOYED
        if intent_type == CapitalIntentType.RETURN.value:
            return CapitalIntentStatus.RETURNED_TO_USDC
        return CapitalIntentStatus.DEPLOYED
    if bridge_status == BridgeJobState.MINT_COMPLETED_TRADE_FAILED.value:
        return CapitalIntentStatus.RETRYABLE
    if bridge_status == BridgeJobState.FAILED.value:
        return CapitalIntentStatus.FAILED
    return CapitalIntentStatus.PENDING


def sync_capital_intents_for_bridge_job(
    bridge_job_id: str,
    bridge_fields: dict,
) -> None:
    """Best-effort sync for intents linked to a bridge_jobs row.

    The bridge relayer is the money movement engine. Intent sync must not block
    that engine, so callers should log and continue if this function fails.
    """
    bridge_status = bridge_fields.get("status")
    if not bridge_status:
        return
    if hasattr(bridge_status, "value"):
        bridge_status = bridge_status.value

    client = get_client()
    result = (
        client.table("capital_movement_intents")
        .select(
            "id, intent_type, status, destination_tx, failure_reason, "
            "arc_receive_tx_hash, arc_finalize_tx_hash"
        )
        .eq("bridge_job_id", bridge_job_id)
        .execute()
    )
    intents = result.data or []
    if not intents:
        return

    for intent in intents:
        next_status = bridge_status_to_intent_status(
            bridge_status,
            intent["intent_type"],
        )
        updates: dict[str, str] = {}
        if intent.get("status") != next_status.value:
            updates["status"] = next_status.value
        if bridge_fields.get("mint_tx_hash") and not intent.get("destination_tx"):
            updates["destination_tx"] = bridge_fields["mint_tx_hash"]
        if bridge_fields.get("trade_tx_hash"):
            updates["destination_tx"] = bridge_fields["trade_tx_hash"]
        if bridge_fields.get("arc_receive_tx_hash"):
            updates["arc_receive_tx_hash"] = bridge_fields["arc_receive_tx_hash"]
        if bridge_fields.get("arc_finalize_tx_hash"):
            updates["arc_finalize_tx_hash"] = bridge_fields["arc_finalize_tx_hash"]
            updates["destination_tx"] = bridge_fields["arc_finalize_tx_hash"]
        for amount_key in (
            "gross_amount_usdc",
            "circle_fee_usdc",
            "net_amount_usdc",
        ):
            if bridge_fields.get(amount_key):
                updates[amount_key] = str(bridge_fields[amount_key])
        if bridge_fields.get("error_message") and not intent.get("failure_reason"):
            updates["failure_reason"] = str(bridge_fields["error_message"])[:500]

        if not updates:
            continue

        client.table("capital_movement_intents").update(updates).eq(
            "id", intent["id"]
        ).execute()
        logger.info(
            "Synced capital intent %s from bridge job %s status=%s",
            intent["id"],
            bridge_job_id,
            bridge_status,
        )
