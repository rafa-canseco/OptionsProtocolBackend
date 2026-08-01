"""Product models for tokenized option funds."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from pydantic.alias_generators import to_camel


class FundModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TokenMetadata(FundModel):
    address: str
    symbol: str
    decimals: int


class ActionAvailability(FundModel):
    available: bool
    reason_code: str | None = None


class FundRegistryItem(FundModel):
    fund_key: str
    strategy_kind: Literal["csp", "covered_call", "meta_wheel"] = "csp"
    chain_id: int
    fund_address: str
    share_token: TokenMetadata
    accounting_asset: TokenMetadata
    quote_asset: TokenMetadata | None = None
    deployment_status: str


class FundListResponse(FundModel):
    funds: list[FundRegistryItem]


class FundComposition(FundModel):
    idle_assets: str
    strategy_accounting_assets: str
    assigned_weth: str
    reserved_claim_assets: str
    gross_assets: str = "0"
    adapter_free_accounting_assets: str = "0"
    locked_collateral_assets: str = "0"
    fair_option_liability_assets: str = "0"
    assigned_weth_value_assets: str = "0"
    settlement_receivable_assets: str = "0"
    settlement_cost_assets: str = "0"
    transient_usdc: str = "0"
    transient_usdc_value_assets: str = "0"
    normalization_cost_assets: str = "0"
    option_exit_cost_assets: str = "0"


class StressNav(FundModel):
    net_assets: str
    share_price_assets: str
    option_liability_assets: str


class NavWindow(FundModel):
    report_nonce: int
    valid_after_block: int | None
    valid_until_block: int | None
    stale: bool
    methodology: str | None = None
    model_version: int | None = None
    observed_at: str | None = None
    source_quality: str | None = None
    stress: StressNav | None = None


class FundStatus(FundModel):
    reconciled: bool
    deposits_paused: bool
    redemptions_paused: bool
    execution_locked: bool
    flow_processing: bool


class FundActions(FundModel):
    deposit: ActionAvailability
    request_redemption: ActionAvailability
    cancel_redemption: ActionAvailability
    claim_redemption: ActionAvailability


class CspPositionSummary(FundModel):
    position_id: int
    lifecycle: str
    strike_price_usd_8: str | None = None
    expiry_timestamp: int | None = None
    option_amount_8: str
    collateral_assets: str
    premium_earned_assets: str
    called_away_usdc: str = "0"
    fallback_weth_recovered_assets: str = "0"
    mm_weth_payout_assets: str = "0"


class StrategyOperationSummary(FundModel):
    operation_type: str
    position_id: int | None = None
    block_number: int | None = None


class FundStrategySnapshot(FundModel):
    strategy_kind: Literal["csp", "covered_call", "meta_wheel"] = "csp"
    latest_position: CspPositionSummary | None = None
    latest_operation: StrategyOperationSummary | None = None
    total_premium_collected_assets: str = "0"
    next_open_after: int | None = None
    next_open_condition: str


class WheelTrancheSummary(FundModel):
    tranche_id: str
    child_vault: str | None = None
    state: str
    principal_assets: str
    child_shares: str
    child_position_id: str | None = None
    assignment_lot_ids: list[str] = Field(default_factory=list)
    literal_assignment_floor_usd_8: str = "0"
    protected_assignment_floor_usd_8: str = "0"
    call_strike_usd_8: str | None = None
    transition_nonce: int
    next_action: str


class MetaWheelSnapshot(FundModel):
    pending_csp_assets: str
    csp_value_assets: str
    transition_weth: str
    transition_weth_value_assets: str
    covered_call_value_assets: str
    returned_usdc_assets: str
    reserved_redemption_assets: str
    active_tranche_count: int
    protected_assignment_floor_usd_8: str
    current_phase: str
    next_action: str
    cumulative_gross_premium_assets: str
    cumulative_protocol_fee_assets: str
    cumulative_net_premium_assets: str
    policy_version: int = 0
    policy_hash: str | None = None
    nav_coherent: bool = False
    nav_snapshot_block: int | None = None
    nav_snapshot_block_hash: str | None = None
    paused: bool = False
    tranches: list[WheelTrancheSummary] = Field(default_factory=list)


class FundSummaryResponse(FundModel):
    fund: FundRegistryItem
    net_assets: str
    share_supply: str
    virtual_shares: str
    share_price_assets: str
    market_price_assets: str | None = None
    stress_price_assets: str | None = None
    composition: FundComposition
    nav: NavWindow
    strategy: FundStrategySnapshot
    status: FundStatus
    actions: FundActions
    as_of_block: int | None
    as_of_block_hash: str | None
    indexed_at: str | None
    stale: bool
    wheel: MetaWheelSnapshot | None = None

    @model_serializer(mode="wrap")
    def omit_empty_wheel(self, handler):
        """Keep the established standalone CSP/CC wire payload byte-stable."""

        data = handler(self)
        if self.wheel is None:
            data.pop("wheel", None)
        return data


class RedemptionView(FundModel):
    pending_shares: str = "0"
    claimable_shares: str = "0"
    claimable_assets: str = "0"
    status: str = "none"
    next_action: str = "none"
    latest_batch_id: int = 0
    latest_batch_processing: bool = False
    latest_batch_unwind_committed: bool = False


class FundPositionResponse(FundModel):
    fund_key: str
    address: str
    shares: str
    accounting_value: str
    redemption: RedemptionView
    actions: FundActions
    as_of_block: int | None
    indexed_at: str | None
    stale: bool


class TrustedContract(FundModel):
    role: str
    address: str
    implementation_address: str | None = None
    interface_version: int


class FundFeePolicy(FundModel):
    management_fee_wad: str
    management_fee_bps: int = Field(ge=0, le=10_000)
    performance_fee_bps: int = Field(ge=0, le=10_000)
    premium_fee_bps: int = Field(ge=0, le=10_000)
    high_water_mark_share_price_assets: str
    fee_recipient: str | None = None
    performance_fee_basis: Literal["high_water_mark"] = "high_water_mark"
    premium_fee_basis: Literal["gross_premium"] = "gross_premium"
    reported_premium_basis: Literal["net_of_premium_fee"] = "net_of_premium_fee"


class FundConfigResponse(FundModel):
    fund_key: str
    deployment_status: str
    contracts: list[TrustedContract]
    fees: FundFeePolicy
    capabilities: FundActions
    writes_enabled: bool
    blocked_reason_code: str | None


class ActivityItem(FundModel):
    activity_type: str
    transaction_hash: str
    block_number: int
    log_index: int
    wallet_address: str | None
    details: dict[str, str | int | bool | None]


class ActivityResponse(FundModel):
    items: list[ActivityItem]
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=100)
