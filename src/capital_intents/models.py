"""Pydantic models for MetaVault capital movement intents."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class CapitalChain(str, Enum):
    ARC = "arc"
    BASE = "base"
    SOLANA = "solana"


class CapitalIntentType(str, Enum):
    DEPOSIT = "deposit"
    DEPLOYMENT = "deployment"
    RETURN = "return"


class MovementReason(str, Enum):
    USER_DEPOSIT = "user_deposit"
    INITIAL_DEPLOYMENT = "initial_deployment"
    ROTATION = "rotation"
    RETURN_TO_ARC = "return_to_arc"
    RETURN_TO_IDLE = "return_to_idle"
    WITHDRAWAL = "withdrawal"


class CapitalIntentStatus(str, Enum):
    PENDING = "pending"
    DEPOSIT_RECEIVED = "deposit_received"
    BRIDGING = "bridging"
    WAITING_TO_BE_DEPLOYED = "waiting_to_be_deployed"
    DEPLOYMENT_IN_FLIGHT = "deployment_in_flight"
    DEPLOYED = "deployed"
    PREMIUM_EARNED = "premium_earned"
    ASSIGNED_ROTATING = "assigned_rotating"
    RETURNING_TO_ARC = "returning_to_arc"
    RETURNED_TO_USDC = "returned_to_usdc"
    CLAIMABLE = "claimable"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYABLE = "retryable"


class UXStatus(str, Enum):
    DEPOSIT_RECEIVED = "Deposit received"
    BRIDGING = "Bridging"
    WAITING_TO_BE_DEPLOYED = "Waiting to be deployed"
    DEPLOYMENT_IN_FLIGHT = "Deployment in flight"
    DEPLOYED_ON_BASE = "Deployed on Base"
    DEPLOYED_ON_SOLANA = "Deployed on Solana"
    PREMIUM_EARNED = "Premium earned"
    ASSIGNED_ROTATING = "Assigned / rotating"
    RETURNING_TO_ARC = "Returning to Arc"
    RETURNED_TO_USDC = "Returned to USDC"
    CLAIMABLE_REDEPLOYABLE = "Claimable / redeployable"


class CapitalIntentCreate(BaseModel):
    intent_type: CapitalIntentType
    movement_reason: MovementReason | None = None
    bucket_id: str | None = None
    receiver: str | None = Field(
        None,
        description="Account that should receive shares or withdrawal credit.",
    )
    source_chain: CapitalChain
    source_account: str
    source_tx: str | None = None
    destination_chain: CapitalChain
    destination_account: str
    destination_tx: str | None = None
    amount_usdc: str = Field(..., description="USDC amount in raw 6-decimal units")
    status: CapitalIntentStatus | None = None
    idempotency_key: str | None = Field(
        None,
        description="Client-provided key preventing duplicate business intents.",
    )
    bridge_job_id: str | None = Field(
        None,
        description="Existing bridge_jobs.id when CCTP execution already exists.",
    )
    create_bridge_job: bool = Field(
        False,
        description="Create a bridge_jobs row for supported Base/Solana CCTP moves.",
    )
    user_id: str | None = Field(
        None,
        description="Privy/user id required only when create_bridge_job=true.",
    )
    quote_id: str | None = Field(
        None,
        description="Optional bridge quote/idempotency key passed to bridge_jobs.",
    )
    signed_trade_tx: str | None = Field(
        None,
        description="Optional destination execution transaction reused by bridge_jobs.",
    )

    @model_validator(mode="after")
    def validate_intent(self):
        if self.source_chain == self.destination_chain:
            raise ValueError("source_chain and destination_chain must differ")
        if int(self.amount_usdc) <= 0:
            raise ValueError("amount_usdc must be greater than zero")
        if self.intent_type == CapitalIntentType.DEPOSIT and not self.receiver:
            raise ValueError("receiver is required for deposit intents")
        if self.create_bridge_job:
            if self.bridge_job_id:
                raise ValueError(
                    "bridge_job_id cannot be provided with create_bridge_job"
                )
            if not self.user_id:
                raise ValueError("user_id is required when create_bridge_job=true")
            if not self.source_tx:
                raise ValueError("source_tx is required when create_bridge_job=true")
            supported = {CapitalChain.BASE, CapitalChain.SOLANA}
            if (
                self.source_chain not in supported
                or self.destination_chain not in supported
            ):
                raise ValueError(
                    "create_bridge_job currently supports only base<->solana moves"
                )
        return self


class CapitalIntentPatch(BaseModel):
    status: CapitalIntentStatus | None = None
    source_tx: str | None = None
    destination_tx: str | None = None
    bridge_job_id: str | None = None
    failure_reason: str | None = None
    completed_at: datetime | None = None


class CapitalIntentResponse(BaseModel):
    id: str
    intent_type: CapitalIntentType
    movement_reason: MovementReason
    bucket_id: str | None = None
    receiver: str | None = None
    source_chain: CapitalChain
    source_account: str
    source_tx: str | None = None
    destination_chain: CapitalChain
    destination_account: str
    destination_tx: str | None = None
    amount_usdc: str
    status: CapitalIntentStatus
    ux_status: str
    bridge_job_id: str | None = None
    idempotency_key: str | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class CapitalIntentCreateResponse(BaseModel):
    intent: CapitalIntentResponse
    bridge_job_created: bool = False
