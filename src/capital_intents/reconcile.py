"""Reconcile settled option positions back into deployable capital intents."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.capital_intents.models import (
    CapitalChain,
    CapitalIntentStatus,
    CapitalIntentType,
    MovementReason,
)
from src.config import settings
from src.db.database import get_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileSummary:
    inspected: int = 0
    skipped_unsettled: int = 0
    waiting_created: int = 0
    already_reconciled: int = 0
    assigned: int = 0
    retryable: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "inspected": self.inspected,
            "skipped_unsettled": self.skipped_unsettled,
            "waiting_created": self.waiting_created,
            "already_reconciled": self.already_reconciled,
            "assigned": self.assigned,
            "retryable": self.retryable,
        }


def reconcile_settled_positions(*, limit: int = 100) -> ReconcileSummary:
    """Create fresh deployment intents for settled OTM positions.

    The agent only consumes `waiting_to_be_deployed` intents. Settlement bots
    mark order_events with outcome data, so this reconciler bridges that state:

    - OTM / not assigned: the venue adapter has idle USDC again, create a new
      rotation intent for the same venue.
    - ITM / assigned: mark the original intent assigned_rotating; CSP redeploy
      must wait for asset-rotation logic.
    - physical_failed: mark retryable for operator attention.
    """
    client = get_client()
    deployed = _read_deployed_intents(client, limit=limit)
    summary = ReconcileSummary(inspected=len(deployed))
    mutable = summary.__dict__.copy()

    for intent in deployed:
        event = _read_order_event_for_intent(client, intent)
        if not event:
            continue
        if not _is_settled(event):
            mutable["skipped_unsettled"] += 1
            continue
        if event.get("settlement_type") == "physical_failed":
            _update_intent_status(
                client,
                intent,
                CapitalIntentStatus.RETRYABLE,
                failure_reason="Physical delivery failed after expiry",
            )
            mutable["retryable"] += 1
            continue
        if _is_assigned(event):
            _update_intent_status(
                client,
                intent,
                CapitalIntentStatus.ASSIGNED_ROTATING,
            )
            mutable["assigned"] += 1
            continue

        idempotency_key = _rotation_idempotency_key(intent, event)
        if _read_existing_rotation(client, idempotency_key):
            _mark_original_completed(client, intent)
            mutable["already_reconciled"] += 1
            continue

        rotation = _rotation_row(intent, event, idempotency_key)
        if rotation is None:
            logger.warning(
                "Cannot reconcile settled intent %s: missing collateral amount",
                intent.get("id"),
            )
            continue
        _insert_rotation(client, rotation)
        _mark_original_completed(client, intent)
        mutable["waiting_created"] += 1

    return ReconcileSummary(**mutable)


def _read_deployed_intents(client: Any, *, limit: int) -> list[dict[str, Any]]:
    result = (
        client.table("capital_movement_intents")
        .select("*")
        .eq("status", CapitalIntentStatus.DEPLOYED.value)
        .limit(limit)
        .execute()
    )
    rows = result.data or []
    return [
        row
        for row in rows
        if row.get("destination_tx")
        and (row.get("selected_chain") or row.get("destination_chain"))
        in {CapitalChain.BASE.value, CapitalChain.SOLANA.value}
    ]


def _read_order_event_for_intent(
    client: Any,
    intent: dict[str, Any],
) -> dict[str, Any] | None:
    tx_hash = intent.get("destination_tx")
    if not tx_hash:
        return None
    result = (
        client.table("order_events")
        .select("*")
        .eq("tx_hash", tx_hash)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _is_settled(event: dict[str, Any]) -> bool:
    if event.get("is_settled") is True:
        return True
    if event.get("settled_at") or event.get("settlement_tx_hash"):
        return True
    return False


def _is_assigned(event: dict[str, Any]) -> bool:
    if event.get("is_itm") is True:
        return True
    return event.get("settlement_type") == "physical"


def _rotation_idempotency_key(intent: dict[str, Any], event: dict[str, Any]) -> str:
    return f"rotation:{intent['id']}:{event.get('tx_hash') or intent.get('destination_tx')}"


def _read_existing_rotation(client: Any, idempotency_key: str) -> dict[str, Any] | None:
    result = (
        client.table("capital_movement_intents")
        .select("id")
        .eq("idempotency_key", idempotency_key)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _rotation_row(
    intent: dict[str, Any],
    event: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    amount = _positive_raw_amount(event.get("collateral"))
    if amount is None:
        return None

    chain = (
        event.get("chain")
        or intent.get("selected_chain")
        or intent.get("destination_chain")
        or CapitalChain.BASE.value
    )
    if chain not in {CapitalChain.BASE.value, CapitalChain.SOLANA.value}:
        return None

    source_account = _venue_account(chain, event, intent)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "intent_type": CapitalIntentType.DEPLOYMENT.value,
        "movement_reason": MovementReason.ROTATION.value,
        "bucket_id": intent.get("bucket_id"),
        "receiver": intent.get("receiver"),
        "source_chain": chain,
        "source_account": source_account,
        "source_tx": event.get("settlement_tx_hash") or event.get("tx_hash"),
        "destination_chain": CapitalChain.ARC.value,
        "destination_account": intent.get("destination_account") or source_account,
        "destination_tx": None,
        "amount_usdc": str(amount),
        "onchain_intent_id": None,
        "status": CapitalIntentStatus.WAITING_TO_BE_DEPLOYED.value,
        "bridge_job_id": None,
        "idempotency_key": idempotency_key,
        "created_at": now,
        "updated_at": now,
}


def _venue_account(
    chain: str,
    event: dict[str, Any],
    intent: dict[str, Any],
) -> str | None:
    if chain == CapitalChain.BASE.value:
        return (
            settings.base_sepolia_vault_adapter
            or event.get("user_address")
            or intent.get("destination_account")
            or intent.get("source_account")
        )
    if chain == CapitalChain.SOLANA.value:
        return (
            settings.solana_devnet_vault_token_account
            or event.get("user_address")
            or intent.get("destination_account")
            or intent.get("source_account")
        )
    return event.get("user_address") or intent.get("destination_account")


def _positive_raw_amount(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _insert_rotation(client: Any, row: dict[str, Any]) -> None:
    result = client.table("capital_movement_intents").insert(row).execute()
    if not result.data:
        raise RuntimeError("capital_movement_intents insert returned no data")


def _mark_original_completed(client: Any, intent: dict[str, Any]) -> None:
    _update_intent_status(client, intent, CapitalIntentStatus.COMPLETED)


def _update_intent_status(
    client: Any,
    intent: dict[str, Any],
    status: CapitalIntentStatus,
    *,
    failure_reason: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "status": status.value,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if failure_reason:
        fields["failure_reason"] = failure_reason
    result = (
        client.table("capital_movement_intents")
        .update(fields)
        .eq("id", intent["id"])
        .execute()
    )
    if not result.data:
        raise RuntimeError(f"capital intent update returned no data: {intent['id']}")
