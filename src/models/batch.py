from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class BatchStatus(str, Enum):
    PENDING = "pending"          # collecting orders
    EXECUTING = "executing"      # settlement in progress
    SETTLED = "settled"          # on-chain settlement complete
    FAILED = "failed"


class Batch(BaseModel):
    id: str | None = None
    status: BatchStatus = BatchStatus.PENDING
    order_count: int = 0
    total_premium: float = 0.0
    created_at: datetime | None = None
    settled_at: datetime | None = None
    tx_hash: str | None = None
