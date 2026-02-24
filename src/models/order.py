import re
from datetime import datetime

from pydantic import BaseModel, Field


ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class OnChainOrder(BaseModel):
    """An indexed OrderExecuted event from BatchSettler.executeOrder()."""
    id: str | None = Field(default=None, description="Database row ID")
    tx_hash: str = Field(description="Transaction hash of the executeOrder call", examples=["0xabc123..."])
    block_number: int = Field(description="Block number where the order was executed", examples=[12345678])
    log_index: int = Field(description="Log index within the transaction", examples=[0])
    user_address: str = Field(description="User's Ethereum address (lowercase)", examples=["0xabcdef0123456789abcdef0123456789abcdef01"])
    otoken_address: str = Field(description="Address of the oToken contract", examples=["0x1234567890abcdef1234567890abcdef12345678"])
    amount: str = Field(description="oToken amount (8 decimals, as string for precision)", examples=["100000000"])
    premium: str = Field(description="Gross premium in USDC (6 decimals, as string)", examples=["42150000"])
    collateral: str = Field(description="Collateral locked (native decimals, as string)", examples=["2400000000"])
    gross_premium: str | None = Field(default=None, description="Total premium before protocol fee (6 decimals)")
    net_premium: str | None = Field(default=None, description="Premium credited to user after fee deduction (6 decimals)")
    protocol_fee: str | None = Field(default=None, description="Fee taken by protocol treasury (6 decimals)")
    vault_id: int = Field(description="On-chain vault ID assigned to this position", examples=[1])
    strike_price: int | None = Field(default=None, description="Strike price (8 decimals)", examples=[240000000000])
    expiry: int | None = Field(default=None, description="Expiry timestamp (unix seconds)", examples=[1709107200])
    is_put: bool | None = Field(default=None, description="True for put, false for call")
    is_settled: bool = Field(default=False, description="Whether this position has been settled")
    settled_at: datetime | None = Field(default=None, description="Timestamp when the position was settled")
    settlement_tx_hash: str | None = Field(default=None, description="Transaction hash of the settlement")
    indexed_at: datetime | None = Field(default=None, description="Timestamp when this event was indexed")
    settlement_type: str | None = Field(default=None, description="Settlement method: 'physical', 'cash', or 'physical_failed'")
    delivered_asset: str | None = Field(default=None, description="Address of asset delivered to user (for physical settlement)")
    delivered_amount: str | None = Field(default=None, description="Amount delivered (native decimals, as string)")
    delivery_tx_hash: str | None = Field(default=None, description="Transaction hash of the physical delivery")
    is_itm: bool | None = Field(default=None, description="Whether the option expired in-the-money")
    expiry_price: str | None = Field(default=None, description="Oracle ETH price at expiry (8 decimals, as string)")
