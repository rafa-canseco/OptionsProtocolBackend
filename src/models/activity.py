from pydantic import BaseModel


class ActivityResponse(BaseModel):
    wallet: str
    totalVolume: float
    totalPremiumEarned: float
    positionCount: int
    activeDays: int
    daysSinceFirst: int
    total_collateral_usd: float
    total_premium_usd: float
    earning_rate: float | None
