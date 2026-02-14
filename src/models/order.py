import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, field_validator


ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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

    @field_validator("user_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        if not ETH_ADDRESS_RE.match(v):
            raise ValueError("Invalid Ethereum address")
        return v

    @field_validator("option_type")
    @classmethod
    def validate_option_type(cls, v: str) -> str:
        if v not in ("call", "put"):
            raise ValueError("option_type must be 'call' or 'put'")
        return v

    @field_validator("strike", "premium", "spot_at_lock")
    @classmethod
    def validate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Must be positive")
        return v

    @field_validator("iv_at_lock")
    @classmethod
    def validate_iv(cls, v: float) -> float:
        if v <= 0 or v > 5.0:
            raise ValueError("IV must be between 0 and 5.0")
        return v

    @field_validator("expiry_days")
    @classmethod
    def validate_expiry(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("expiry_days must be positive")
        return v


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
