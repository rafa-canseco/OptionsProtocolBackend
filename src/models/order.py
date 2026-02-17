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
    premium: str         # raw USDC premium (6 decimals) as string
    collateral: str      # raw collateral as string
    vault_id: int
    strike_price: float | None = None
    expiry: int | None = None
    is_put: bool | None = None
    is_settled: bool = False
    settled_at: datetime | None = None
    settlement_tx_hash: str | None = None
    indexed_at: datetime | None = None

    # Physical settlement fields
    settlement_type: str | None = None       # "physical" or "cash"
    delivered_asset: str | None = None       # address of asset delivered to user
    delivered_amount: str | None = None      # raw amount delivered
    delivery_tx_hash: str | None = None      # tx hash of physical delivery
    is_itm: bool | None = None               # whether option expired in-the-money
    expiry_price: str | None = None          # oracle ETH price at expiry
