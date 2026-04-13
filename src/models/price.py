from pydantic import BaseModel, Field

from src.pricing.black_scholes import OptionType


class PriceResponse(BaseModel):
    option_type: OptionType = Field(description="PUT or CALL")
    strike: float = Field(description="Strike price in USD", examples=[2400.0])
    expiry_days: int = Field(
        description="Days until expiry (cosmetic, drifts daily)", examples=[7]
    )
    expiry_date: str | None = Field(
        default=None,
        description="Expiry date as ISO string (e.g. 2026-03-07)",
        examples=["2026-03-07"],
    )
    premium: float = Field(
        description="Net premium per contract in USD (after protocol fee)",
        examples=[42.15],
    )
    delta: float = Field(
        description="Option delta (absolute value, 0-1)", examples=[0.25]
    )
    iv: float = Field(
        description="Implied volatility used for pricing (annualized, e.g. 0.65 = 65%)",
        examples=[0.65],
    )
    spot: float = Field(
        description="ETH spot price at time of quote (USD)", examples=[2650.0]
    )
    ttl: int = Field(description="Seconds until this quote expires", examples=[30])
    expires_at: float = Field(
        description="Unix timestamp when this quote expires", examples=[1708776000.0]
    )
    available_amount: float = Field(
        description="Maximum notional amount available at this price (in ETH)",
        examples=[1000.0],
    )
    otoken_address: str | None = Field(
        default=None,
        description="On-chain oToken contract address (null if not yet created)",
        examples=["0xAbC1230000000000000000000000000000000000"],
    )
    # EIP-712 signed quote fields (needed by frontend for executeOrder)
    signature: str | None = Field(
        default=None,
        description="EIP-712 signature for this quote (hex, 0x-prefixed)",
    )
    mm_address: str | None = Field(
        default=None,
        description="Market maker address that signed this quote",
    )
    bid_price_raw: int | None = Field(
        default=None,
        description="Bid price in USDC smallest units (6 decimals)",
    )
    deadline: int | None = Field(
        default=None,
        description="Quote deadline as Unix timestamp",
    )
    quote_id: str | None = Field(
        default=None,
        description="Unique quote identifier (maps to quoteId in the contract)",
    )
    max_amount_raw: int | None = Field(
        default=None,
        description="Maximum oToken amount in on-chain units (8 decimals)",
    )
    maker_nonce: int | None = Field(
        default=None,
        description="MM's makerNonce at time of signing",
    )
    chain: str = Field(
        default="base",
        description="Chain this quote is on (base or solana)",
    )
    position_count: int = Field(
        default=0,
        description=(
            "Active positions for this (strike, option_type) pair, "
            "scaled by ACTIVITY_MULTIPLIER for social proof display"
        ),
    )
