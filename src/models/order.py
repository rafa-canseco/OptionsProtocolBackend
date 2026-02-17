import re
from datetime import datetime

from pydantic import BaseModel


ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class OnChainOrder(BaseModel):
    """An indexed OrderExecuted event from BatchSettler.executeOrder()."""
    id: str | None = None
    tx_hash: str
    block_number: int
    log_index: int
    user_address: str
    otoken_address: str
    amount: str          # raw oToken amount (8 decimals) as string for precision
    premium: str         # raw gross premium (6 decimals) — kept for backwards compat
    collateral: str      # raw collateral as string
    gross_premium: str | None = None   # total premium before protocol fee
    net_premium: str | None = None     # premium credited to user after fee deduction
    protocol_fee: str | None = None    # fee taken by protocol treasury
    vault_id: int
    strike_price: int | None = None
    expiry: int | None = None
    is_put: bool | None = None
    is_settled: bool = False
    settled_at: datetime | None = None
    settlement_tx_hash: str | None = None
    indexed_at: datetime | None = None

    # Physical settlement fields
    settlement_type: str | None = None       # "physical", "cash", or "physical_failed"
    delivered_asset: str | None = None       # address of asset delivered to user
    delivered_amount: str | None = None      # raw amount delivered
    delivery_tx_hash: str | None = None      # tx hash of physical delivery
    is_itm: bool | None = None               # whether option expired in-the-money
    expiry_price: str | None = None          # oracle ETH price at expiry
