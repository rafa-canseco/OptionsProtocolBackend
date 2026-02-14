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
