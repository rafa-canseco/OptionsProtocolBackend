from pydantic import BaseModel, Field


class QuoteSubmission(BaseModel):
    """A single EIP-712 signed quote from a market maker."""

    otoken_address: str = Field(description="oToken contract address")
    bid_price: int = Field(description="Bid price in USDC (6 decimals)")
    deadline: int = Field(description="Unix timestamp after which the quote expires")
    quote_id: int = Field(description="Unique quote identifier per MM")
    max_amount: int = Field(description="Maximum oToken amount (8 decimals)")
    maker_nonce: int = Field(description="MM's current makerNonce from BatchSettler")
    signature: str = Field(description="EIP-712 signature (hex, 0x-prefixed)")
    # Optional metadata for display (not part of EIP-712 struct)
    strike_price: float | None = Field(default=None, description="Strike price in USD")
    expiry: int | None = Field(default=None, description="Expiry timestamp")
    is_put: bool | None = Field(default=None, description="True for put, false for call")


class QuoteBatchRequest(BaseModel):
    """Batch of signed quotes submitted by a market maker."""

    quotes: list[QuoteSubmission] = Field(min_length=1, max_length=100)


class QuoteBatchResponse(BaseModel):
    """Response after submitting a batch of quotes."""

    accepted: int = Field(description="Number of quotes accepted")
    rejected: int = Field(description="Number of quotes rejected")
    errors: list[str] = Field(default_factory=list, description="Rejection reasons")


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
