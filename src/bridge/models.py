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


class BridgeJobReserveRequest(BaseModel):
    source_chain: BridgeChain
    dest_chain: BridgeChain
    user_id: str = Field(..., description="Privy user ID")
    mint_recipient: str = Field(
        ..., description="Destination wallet address to receive USDC"
    )
    burn_amount: str = Field(..., description="Expected USDC amount to burn")
    quote_id: str = Field(..., description="Quote ID to reserve before burn")
    signed_trade_tx: str | None = Field(
        None,
        description="Pre-signed destination trade transaction. None = bridge only.",
    )


class SolanaCCTPBurnPrepareRequest(BaseModel):
    owner: str = Field(..., description="Solana wallet that owns the USDC ATA")
    dest_chain: BridgeChain = Field(
        BridgeChain.BASE,
        description="Destination chain. Phase 1 supports Solana source burns.",
    )
    mint_recipient: str = Field(
        ..., description="Destination wallet address to receive minted USDC"
    )
    burn_amount: str = Field(..., description="USDC amount to burn (raw, 6 decimals)")
    max_fee: str = Field(
        "0",
        description="CCTP max fee in USDC raw units. Use Circle fee API for fast burns.",
    )
    min_finality_threshold: int = Field(
        2000,
        description="CCTP finality threshold. 2000 is standard transfer.",
    )
    destination_caller: str | None = Field(
        None,
        description="Optional 32-byte destination caller encoded as chain address.",
    )


class SolanaCCTPBurnPrepareResponse(BaseModel):
    transaction_base64: str
    message_sent_event_data: str
    fee_payer: str
    owner: str
    burn_token_account: str
    source_chain: BridgeChain
    dest_chain: BridgeChain
    source_domain: int
    destination_domain: int
    burn_amount: str
    max_fee: str
    min_finality_threshold: int


class SolanaCCTPBurnSubmitRequest(BaseModel):
    signed_transaction_base64: str = Field(
        ..., description="Prepared burn tx after frontend adds the owner signature"
    )
    dest_chain: BridgeChain = BridgeChain.BASE
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
        description="Pre-signed destination trade transaction. None = bridge only.",
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
