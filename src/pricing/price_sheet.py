import time
from dataclasses import dataclass

from src.config import settings
from src.pricing.black_scholes import OptionType, price, delta


@dataclass
class PriceQuote:
    option_type: OptionType
    strike: float
    expiry_days: int
    premium: float
    delta: float
    iv: float
    spot: float
    created_at: float  # unix timestamp
    ttl: int  # seconds until quote expires

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


def generate_strikes(spot: float, num_strikes: int = 5) -> list[float]:
    """Generate strike prices around the current spot.

    Rounds to the nearest $50 for ETH-sized prices.
    """
    step = 50.0
    center = round(spot / step) * step
    half = num_strikes // 2
    return [center + (i - half) * step for i in range(num_strikes)]


def generate_price_sheet(
    spot: float,
    iv: float,
    expiry_days: list[int] | None = None,
    num_strikes: int = 5,
) -> list[PriceQuote]:
    """Generate a full price menu of calls and puts.

    Args:
        spot: Current ETH price
        iv: Implied volatility (annualized decimal, e.g. 0.80)
        expiry_days: List of expiry windows in days. Defaults to [7, 14, 30]
        num_strikes: Number of strikes to generate around ATM
    """
    if expiry_days is None:
        expiry_days = [7, 14, 30]

    r = settings.risk_free_rate
    strikes = generate_strikes(spot, num_strikes)
    now = time.time()
    quotes: list[PriceQuote] = []

    for days in expiry_days:
        T = days / 365.0
        for K in strikes:
            for opt_type in (OptionType.CALL, OptionType.PUT):
                premium = price(opt_type, spot, K, T, r, iv)
                d = delta(opt_type, spot, K, T, r, iv)
                quotes.append(
                    PriceQuote(
                        option_type=opt_type,
                        strike=K,
                        expiry_days=days,
                        premium=round(premium, 2),
                        delta=round(d, 4),
                        iv=iv,
                        spot=spot,
                        created_at=now,
                        ttl=settings.price_ttl_seconds,
                    )
                )

    return quotes
