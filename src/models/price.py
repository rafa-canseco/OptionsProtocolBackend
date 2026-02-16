from pydantic import BaseModel

from src.pricing.black_scholes import OptionType


class PriceResponse(BaseModel):
    option_type: OptionType
    strike: float
    expiry_days: int
    premium: float
    delta: float
    iv: float
    spot: float
    ttl: int
    expires_at: float
    available_amount: float  # max notional available at this price
    otoken_address: str | None = None  # on-chain oToken address, null if not yet created
