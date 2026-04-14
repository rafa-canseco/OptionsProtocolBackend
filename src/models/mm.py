import re

from pydantic import BaseModel, Field, field_validator, model_validator

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
VALID_ASSETS = {"eth", "btc", "sol", "okb"}
HEX_SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
VALID_CHAINS = {"base", "solana", "xlayer"}
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
# ed25519 signatures are 64 bytes; base58-encoded they reach up to 88 chars
BASE58_SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,88}$")


class QuoteSubmission(BaseModel):
    """A signed quote from a market maker (EIP-712 for Base, ed25519 for Solana)."""

    otoken_address: str = Field(description="oToken contract address")
    bid_price: int = Field(
        ge=1, description="Bid price in USDC smallest units (1e6 = 1 USDC)"
    )
    deadline: int = Field(
        gt=0, description="Unix timestamp after which the quote expires"
    )
    quote_id: int = Field(ge=0, description="Unique quote identifier per MM")
    max_amount: int = Field(
        ge=1, description="Maximum oToken amount in smallest units (1e8 = 1 oToken)"
    )
    maker_nonce: int = Field(
        ge=0, description="MM's current makerNonce from BatchSettler"
    )
    signature: str = Field(description="EIP-712 signature (hex, 0x-prefixed, 65 bytes)")
    chain: str = Field(
        default="base",
        description="Chain this quote is for (base, solana)",
    )
    maker: str | None = Field(
        default=None,
        description="Solana maker pubkey (base58). Required when chain=solana.",
    )
    # Optional metadata for display (not part of EIP-712 struct)
    asset: str = Field(default="eth", description="Underlying asset (eth, btc)")
    strike_price: float | None = Field(
        default=None, ge=0, description="Strike price in USD"
    )
    expiry: int | None = Field(default=None, gt=0, description="Expiry timestamp")
    is_put: bool | None = Field(
        default=None, description="True for put, false for call"
    )

    @field_validator("asset")
    @classmethod
    def validate_asset(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_ASSETS:
            raise ValueError(f"asset must be one of {VALID_ASSETS}")
        return v

    @field_validator("chain")
    @classmethod
    def validate_chain(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_CHAINS:
            raise ValueError(f"chain must be one of {VALID_CHAINS}")
        return v

    @model_validator(mode="after")
    def validate_chain_specific_fields(self) -> "QuoteSubmission":
        if self.chain == "solana":
            if not self.maker:
                raise ValueError("maker (Solana pubkey) is required when chain=solana")
            if not BASE58_RE.match(self.maker):
                raise ValueError("maker must be a valid base58 Solana address")
            if not BASE58_RE.match(self.otoken_address):
                raise ValueError("otoken_address must be base58 when chain=solana")
            if not BASE58_SIG_RE.match(self.signature):
                raise ValueError(
                    "signature must be a base58-encoded ed25519 signature "
                    "when chain=solana"
                )
        else:
            self.otoken_address = self.otoken_address.lower()
            if not ETH_ADDRESS_RE.match(self.otoken_address):
                raise ValueError(
                    "otoken_address must be 0x-prefixed ETH address when chain=base"
                )
            if not self.signature.startswith("0x"):
                self.signature = f"0x{self.signature}"
            if not HEX_SIGNATURE_RE.match(self.signature):
                raise ValueError(
                    "signature must be 0x-prefixed hex (65 bytes) when chain=base"
                )
        return self


class QuoteBatchRequest(BaseModel):
    """Batch of signed quotes submitted by a market maker."""

    quotes: list[QuoteSubmission] = Field(min_length=1, max_length=200)


class QuoteBatchResponse(BaseModel):
    """Response after submitting a batch of quotes."""

    accepted: int = Field(description="Number of quotes accepted")
    rejected: int = Field(description="Number of quotes rejected")
    errors: list[str] = Field(default_factory=list, description="Rejection reasons")


class FillResponse(BaseModel):
    """A single fill (OrderExecuted) for the MM."""

    tx_hash: str
    chain: str = "base"
    tx_url: str | None = None
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

    asset: str = Field(description="Asset symbol (eth, btc, sol)")
    spot: float = Field(description="Spot price in USD")
    iv: float = Field(description="Implied volatility (annualized decimal)")
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
    asset: str = "eth"
    strike_price: float | None = None
    expiry: int | None = None
    is_put: bool | None = None
    is_active: bool
    created_at: str


class CapacityUpdateRequest(BaseModel):
    """Capacity report from a market maker."""

    asset: str = Field(default="eth", description="Asset symbol (eth, btc, sol)")
    capacity_eth: float = Field(ge=0, description="Available capacity in native units")
    capacity_usd: float = Field(ge=0, description="Available capacity in USD")
    status: str = Field(description="active, degraded, or full")
    # Optional internal fields (sent by internal MMs)
    premium_pool_usd: float | None = None
    hedge_pool_usd: float | None = None
    hedge_pool_withdrawable_usd: float | None = None
    leverage: int | None = None
    open_positions_count: int | None = None
    open_positions_notional_usd: float | None = None

    @field_validator("asset")
    @classmethod
    def validate_asset(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_ASSETS:
            raise ValueError(f"asset must be one of {VALID_ASSETS}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"active", "degraded", "full"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class CapacityResponse(BaseModel):
    """Public capacity info exposed to the frontend."""

    asset: str = Field(description="Asset symbol (eth, btc, sol)")
    capacity: float = Field(description="Total available capacity in native units")
    capacity_usd: float = Field(description="Total available capacity in USD")
    market_open: bool = Field(description="Whether any MM is accepting positions")
    market_status: str = Field(description="active, degraded, or full")
    max_position: float = Field(description="Max single position size in native units")
    mm_count: int = Field(description="Number of active MMs reporting")
    updated_at: str = Field(description="Latest report timestamp (ISO 8601)")
