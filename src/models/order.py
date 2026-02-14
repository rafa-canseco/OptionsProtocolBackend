from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class OrderStatus(str, Enum):
    PENDING = "pending"          # accepted by user, waiting for batch
    BATCHED = "batched"          # grouped into a batch
    SETTLED = "settled"          # on-chain settlement complete
    EXPIRED = "expired"          # TTL expired before batch
    FAILED = "failed"            # settlement failed


class AcceptOrderRequest(BaseModel):
    """What the user sends when accepting a price."""
    user_address: str
    option_type: str  # "call" or "put"
    strike: float
    expiry_days: int
    premium: float
    spot_at_lock: float
    iv_at_lock: float


class Order(BaseModel):
    id: str | None = None
    user_address: str
    option_type: str
    strike: float
    expiry_days: int
    premium: float
    spot_at_lock: float
    iv_at_lock: float
    status: OrderStatus = OrderStatus.PENDING
    batch_id: str | None = None
    created_at: datetime | None = None
    settled_at: datetime | None = None
    tx_hash: str | None = None
