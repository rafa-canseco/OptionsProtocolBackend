"""Pydantic models for the bridge relayer."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class BridgeChain(str, Enum):
    BASE = "base"
    SOLANA = "solana"


class BridgeJobState(str, Enum):
    PENDING = "pending"
    ATTESTING = "attesting"
    MINTING = "minting"
    TRADING = "trading"
    COMPLETED = "completed"
    MINT_COMPLETED = "mint_completed"
    FAILED = "failed"
    MINT_COMPLETED_TRADE_FAILED = "mint_completed_trade_failed"


class BridgeAndTradeRequest(BaseModel):
    burn_tx_hash: str = Field(
        ..., description="Tx hash of the depositForBurn on source chain"
    )
    source_chain: BridgeChain
    dest_chain: BridgeChain
    user_id: str = Field(..., description="Privy user ID")
    mint_recipient: str = Field(
        ..., description="Destination wallet address to receive USDC"
    )
    burn_amount: str = Field(..., description="USDC amount burned (raw, 6 decimals)")
    quote_id: str | None = Field(
        None,
        description="Quote ID for deduplication. Rejects if a job "
        "with this quote_id already exists.",
    )
    signed_trade_tx: str | None = Field(
        None,
        description="Pre-signed trade transaction (hex for EVM, "
        "base64 for Solana). Backend submits as-is after mint. "
        "None = bridge only, no trade.",
    )


class BridgeJobStatus(BaseModel):
    id: str
    status: BridgeJobState
    source_chain: BridgeChain
    dest_chain: BridgeChain
    burn_tx_hash: str
    burn_amount: str
    mint_recipient: str
    quote_id: str | None = None
    mint_tx_hash: str | None = None
    trade_tx_hash: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
