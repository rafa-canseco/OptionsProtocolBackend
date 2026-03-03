from dataclasses import dataclass

from src.pricing.black_scholes import OptionType
from src.pricing.utils import get_friday_expiries


@dataclass
class OTokenSpec:
    """Specification for an oToken to create on-chain."""

    option_type: OptionType
    strike: float
    expiry_ts: int


def generate_strikes(spot: float, num_strikes: int = 5) -> list[float]:
    """Generate strike prices around the current spot.

    Rounds to the nearest $50 for ETH-sized prices.
    """
    step = 50.0
    center = round(spot / step) * step
    half = num_strikes // 2
    return [center + (i - half) * step for i in range(num_strikes)]


def generate_otoken_specs(
    spot: float,
    expiry_timestamps: list[int] | None = None,
    num_strikes: int = 5,
) -> list[OTokenSpec]:
    """Generate the set of oTokens to list (strikes x expiries x types).

    Args:
        spot: Current ETH price (used to center strikes)
        expiry_timestamps: Fixed Friday 08:00 UTC timestamps.
            Defaults to get_friday_expiries().
        num_strikes: Number of strikes to generate around ATM
    """
    if expiry_timestamps is None:
        expiry_timestamps = get_friday_expiries()

    strikes = generate_strikes(spot, num_strikes)
    specs: list[OTokenSpec] = []

    for ts in expiry_timestamps:
        for K in strikes:
            for opt_type in (OptionType.CALL, OptionType.PUT):
                specs.append(
                    OTokenSpec(
                        option_type=opt_type,
                        strike=K,
                        expiry_ts=ts,
                    )
                )

    return specs
