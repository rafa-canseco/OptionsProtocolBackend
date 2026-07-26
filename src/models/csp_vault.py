"""Product models for tokenized CSP funds."""

from pydantic import BaseModel, ConfigDict, Field
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
    chain_id: int
    fund_address: str
    share_token: TokenMetadata
    accounting_asset: TokenMetadata
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
    status: FundStatus
    actions: FundActions
    as_of_block: int | None
    as_of_block_hash: str | None
    indexed_at: str | None
    stale: bool


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


class FundConfigResponse(FundModel):
    fund_key: str
    deployment_status: str
    contracts: list[TrustedContract]
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
