import re

from pydantic import BaseModel, Field, field_validator

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX_SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")


class QuoteSubmission(BaseModel):
    """A single EIP-712 signed quote from a market maker."""

    otoken_address: str = Field(description="oToken contract address")
    bid_price: int = Field(ge=1, description="Bid price in USDC smallest units (1e6 = 1 USDC)")
    deadline: int = Field(gt=0, description="Unix timestamp after which the quote expires")
    quote_id: int = Field(ge=0, description="Unique quote identifier per MM")
    max_amount: int = Field(ge=1, description="Maximum oToken amount in smallest units (1e8 = 1 oToken)")
    maker_nonce: int = Field(ge=0, description="MM's current makerNonce from BatchSettler")
    signature: str = Field(description="EIP-712 signature (hex, 0x-prefixed, 65 bytes)")
    # Optional metadata for display (not part of EIP-712 struct)
    strike_price: float | None = Field(default=None, ge=0, description="Strike price in USD")
    expiry: int | None = Field(default=None, gt=0, description="Expiry timestamp")
    is_put: bool | None = Field(default=None, description="True for put, false for call")

    @field_validator("otoken_address")
    @classmethod
    def validate_eth_address(cls, v: str) -> str:
        if not ETH_ADDRESS_RE.match(v):
            raise ValueError("Must be a 0x-prefixed, 40-hex-char Ethereum address")
        return v.lower()

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, v: str) -> str:
        if not v.startswith("0x"):
            v = f"0x{v}"
        if not HEX_SIGNATURE_RE.match(v):
            raise ValueError("Must be a 0x-prefixed hex string of 65 bytes (132 chars)")
        return v


class QuoteBatchRequest(BaseModel):
    """Batch of signed quotes submitted by a market maker."""

    quotes: list[QuoteSubmission] = Field(min_length=1, max_length=100)


class QuoteBatchResponse(BaseModel):
    """Response after submitting a batch of quotes."""

    accepted: int = Field(description="Number of quotes accepted")
    rejected: int = Field(description="Number of quotes rejected")
    errors: list[str] = Field(default_factory=list, description="Rejection reasons")


class FillResponse(BaseModel):
    """A single fill (OrderExecuted) for the MM."""

    tx_hash: str
    block_number: int
    otoken_address: str
    amount: str
    gross_premium: str
    net_premium: str
    protocol_fee: str
    collateral: str
    user_address: str
    vault_id: int
    strike_price: float | None = None
    expiry: int | None = None
    is_put: bool | None = None
    indexed_at: str


class PositionGroup(BaseModel):
    """Open positions grouped by oToken."""

    otoken_address: str
    strike_price: float
    expiry: int
    is_put: bool
    total_amount: str
    total_premium_earned: str
    fill_count: int


class ExpiryBucket(BaseModel):
    """Positions grouped by expiry date."""

    expiry: int
    position_count: int
    total_amount: str


class ExposureResponse(BaseModel):
    """Aggregated risk summary for the MM."""

    active_quotes_count: int
    active_quotes_notional: str
    open_positions_by_expiry: list[ExpiryBucket]
    total_premium_earned: str
    pending_settlement_count: int


class OTokenInfo(BaseModel):
    """Available oToken metadata for pricing."""

    address: str
    strike_price: float
    expiry: int
    is_put: bool


class MarketDataResponse(BaseModel):
    """Market data for MM's pricing engine."""

    eth_spot: float
    eth_iv: float
    protocol_fee_bps: int
    gas_price_gwei: float
    available_otokens: list[OTokenInfo]


class QuoteResponse(BaseModel):
    """A single active quote as returned by GET /mm/quotes."""

    id: str
    otoken_address: str
    bid_price: str
    deadline: int
    quote_id: str
    max_amount: str
    maker_nonce: int
    signature: str
    strike_price: float | None = None
    expiry: int | None = None
    is_put: bool | None = None
    is_active: bool
    created_at: str
