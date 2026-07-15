"""Product-level API models for the v2 ETH/USDC CSP vault."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CspModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TokenMetadata(CspModel):
    symbol: str
    address: str
    decimals: int


class VaultAssets(CspModel):
    deposit: TokenMetadata
    assigned: TokenMetadata


class VaultSummary(CspModel):
    total_managed_assets: str
    total_shares: str
    share_price_assets: str
    available_idle_assets: str
    active_collateral: str
    active_batch_count: int
    utilization_bps: int
    pending_deposit_assets: str
    pending_withdrawal_shares: str
    accounted_underlying_assets: str


class CspBatchView(CspModel):
    batch_id: int
    protocol_vault_id: int
    epoch_id: int
    status: str
    settlement_classification: str = "provisional"
    o_token: str
    strike_price: str
    expiry: int
    amount: str
    collateral: str
    premium_earned: str
    collateral_returned: str
    underlying_received: str
    assignment_shortfall: str


class CurrentCycle(CspModel):
    epoch_id: int
    status: str
    started_at: int
    ended_at: int | None
    premium_earned: str
    performance_fee: str
    assignment_shortfall: str
    closed: bool
    batches_truncated: bool
    batches: list[CspBatchView]


class VaultResponse(CspModel):
    vault_key: str
    chain_id: int
    vault_address: str
    assets: VaultAssets
    status: str
    summary: VaultSummary
    current_cycle: CurrentCycle
    as_of_block: int
    indexed_at: str
    finality: str = "head"
    stale: bool = False


class WithdrawalPosition(CspModel):
    epoch_id: int | None
    shares: str
    claimable: bool
    usdc_assets: str
    weth_assets: str


class UserPosition(CspModel):
    active_shares: str
    active_assets: str
    pending_deposit_assets: str
    withdrawal: WithdrawalPosition
    claimable_assigned_weth: str


class ActionAvailability(CspModel):
    available: bool
    reason: str | None = None
    mode: str | None = None


class UserActions(CspModel):
    deposit: ActionAvailability
    cancel_pending_deposit: ActionAvailability
    withdraw_idle: ActionAvailability
    request_withdraw: ActionAvailability
    claim_withdraw: ActionAvailability
    claim_assigned_weth: ActionAvailability


class UserPositionResponse(CspModel):
    vault_key: str
    chain_id: int
    vault_address: str
    address: str
    position: UserPosition
    actions: UserActions
    as_of_block: int
    indexed_at: str
    finality: str = "head"
    stale: bool = False
