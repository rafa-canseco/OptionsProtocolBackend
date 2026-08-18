from pydantic import BaseModel


class YieldAssetSummary(BaseModel):
    asset: str
    pending_raw: int
    pending: float
    delivered_raw: int
    delivered: float
    estimated_accruing_raw: int
    estimated_accruing: float
    total_raw: int
    total: float


class YieldSummaryResponse(BaseModel):
    wallet: str
    assets: list[YieldAssetSummary]
    as_of: str
    accrued_as_of: str


class YieldPosition(BaseModel):
    id: str
    vault_id: int
    asset: str
    collateral_amount: int
    deposited_at: str
    settled_at: str | None
    is_active: bool
    estimated_yield_raw: int
    estimated_yield: float


class YieldPositionTotal(BaseModel):
    asset: str
    estimated_yield_raw: int
    estimated_yield: float


class YieldPositionsResponse(BaseModel):
    wallet: str
    positions: list[YieldPosition]
    totals: list[YieldPositionTotal]
    limit: int
    has_more: bool
    next_cursor: str | None
    as_of: str
    accrued_as_of: str


class YieldHistoryEntry(BaseModel):
    id: str
    distribution_id: str
    asset: str
    amount_raw: int
    amount: float
    status: str
    airdrop_tx_hash: str | None
    created_at: str


class YieldHistoryResponse(BaseModel):
    wallet: str
    history: list[YieldHistoryEntry]
    limit: int
    has_more: bool
    next_cursor: str | None
    as_of: str


class YieldStatsAsset(BaseModel):
    asset: str
    total_yield_raw: int
    total_yield: float
    total_fees_raw: int
    total_fees: float
    total_distributed: float
    distributions: int
    current_accrued_raw: int | None
    current_accrued: float | None


class YieldStatsResponse(BaseModel):
    assets: list[YieldStatsAsset]
    as_of: str
    accrued_as_of: str
