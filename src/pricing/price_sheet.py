from dataclasses import dataclass

from src.pricing.assets import Asset, get_asset_config
from src.pricing.black_scholes import OptionType
from src.pricing.utils import get_friday_expiries


@dataclass
class OTokenSpec:
    """Specification for an oToken to create on-chain."""

    option_type: OptionType
    strike: float
    expiry_ts: int

    def __post_init__(self):
        if self.expiry_ts % 86400 != 28800:
            raise ValueError(
                f"expiry_ts {self.expiry_ts} is not at 08:00 UTC "
                f"(ts % 86400 = {self.expiry_ts % 86400}, expected 28800)"
            )


def generate_strikes(
    spot: float,
    step: float = 50.0,
    num_strikes: int = 5,
) -> list[float]:
    """Generate strike prices around the current spot.

    Args:
        spot: Current price of the underlying asset.
        step: Rounding step for strikes (e.g. $50 for ETH, $1000 for BTC).
        num_strikes: Total number of strikes to generate.
    """
    center = round(spot / step) * step
    half = num_strikes // 2
    return [center + (i - half) * step for i in range(num_strikes)]


def generate_otoken_specs(
    spot: float,
    asset: Asset = Asset.ETH,
    expiry_timestamps: list[int] | None = None,
    num_strikes: int | None = None,
) -> list[OTokenSpec]:
    """Generate the set of oTokens to list (strikes x expiries x types).

    Args:
        spot: Current price (used to center strikes).
        asset: Which underlying asset this is for.
        expiry_timestamps: Fixed 08:00 UTC timestamps.
            Defaults to get_friday_expiries().
        num_strikes: Override number of strikes (defaults to asset config).
    """
    cfg = get_asset_config(asset)
    if expiry_timestamps is None:
        expiry_timestamps = get_friday_expiries()
    if num_strikes is None:
        num_strikes = cfg.num_strikes

    strikes = generate_strikes(spot, step=cfg.strike_step, num_strikes=num_strikes)
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
