import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from scipy.stats import norm

from src.pricing.assets import Asset, get_asset_config
from src.pricing.black_scholes import OptionType
from src.pricing.utils import cutoff_hours_for_expiry, get_expiries

_CSP_ROLLING_MIN_SECONDS = 36 * 3600
_CSP_ROLLING_MAX_SECONDS = 60 * 3600
_CSP_STRIKE_OTM_BPS = 1500
_CSP_STRIKE_TICK_USD = 25
_COVERED_CALL_ROLLING_MIN_SECONDS = 36 * 3600
_COVERED_CALL_ROLLING_MAX_SECONDS = 61 * 3600
_COVERED_CALL_TARGET_DELTA = 0.05
_COVERED_CALL_STRIKE_TICK_USD = 5


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
    min_otm_per_side: int = 4,
) -> list[float]:
    """Generate strike prices around the current spot.

    Starts with ``num_strikes`` centered on spot, then extends in
    each direction until at least ``min_otm_per_side`` OTM strikes
    exist on both the put side (below spot) and call side (above).
    """
    center = round(spot / step) * step
    half = num_strikes // 2
    strikes = [center + (i - half) * step for i in range(num_strikes)]

    lowest = min(strikes)
    while sum(1 for s in strikes if s < spot) < min_otm_per_side:
        lowest -= step
        strikes.append(lowest)

    highest = max(strikes)
    while sum(1 for s in strikes if s > spot) < min_otm_per_side:
        highest += step
        strikes.append(highest)

    return sorted(strikes)


def covered_call_target_strike(
    *,
    spot: float,
    ttl_seconds: int,
    iv: float,
    risk_free_rate: float,
    target_delta: float = _COVERED_CALL_TARGET_DELTA,
    strike_tick_usd: int = _COVERED_CALL_STRIKE_TICK_USD,
) -> float:
    """Solve the Black-Scholes call-delta strike and snap to the policy tick."""
    if spot <= 0 or ttl_seconds <= 0 or iv <= 0:
        raise ValueError("Covered-call target inputs must be positive")
    if not 0 < target_delta < 0.5 or strike_tick_usd <= 0:
        raise ValueError("Covered-call target policy is invalid")
    time_years = ttl_seconds / (365 * 86_400)
    target_d1 = float(norm.ppf(target_delta))
    theoretical = spot * math.exp(
        (risk_free_rate + 0.5 * iv**2) * time_years
        - target_d1 * iv * math.sqrt(time_years)
    )
    ticks = (
        Decimal(str(theoretical)) / Decimal(str(strike_tick_usd))
    ).to_integral_value(rounding=ROUND_HALF_UP)
    return float(ticks * Decimal(str(strike_tick_usd)))


def generate_otoken_specs(
    spot: float,
    asset: Asset = Asset.ETH,
    expiry_timestamps: list[int] | None = None,
    num_strikes: int | None = None,
    iv: float | None = None,
    risk_free_rate: float = 0.05,
) -> list[OTokenSpec]:
    """Generate the set of oTokens to list (strikes x expiries x types).

    Args:
        spot: Current price (used to center strikes).
        asset: Which underlying asset this is for.
        expiry_timestamps: Fixed 08:00 UTC timestamps.
            Defaults to get_expiries().
        num_strikes: Override number of strikes (defaults to asset config).
    """
    cfg = get_asset_config(asset)
    if expiry_timestamps is None:
        expiry_timestamps = get_expiries()
    else:
        now_ts = int(time.time())
        expiry_timestamps = [
            ts
            for ts in expiry_timestamps
            if ts > now_ts + cutoff_hours_for_expiry(ts, now_ts) * 3600
        ]
    if num_strikes is None:
        num_strikes = cfg.num_strikes

    # The smallest timestamp is the 1-day slot — use tighter steps
    daily_ts = min(expiry_timestamps) if expiry_timestamps else None
    now_ts = int(time.time())
    specs: list[OTokenSpec] = []

    for ts in expiry_timestamps:
        is_daily = ts == daily_ts and (ts - now_ts) <= 48 * 3600
        step = cfg.short_expiry_strike_step if is_daily else cfg.strike_step
        strikes = generate_strikes(
            spot,
            step=step,
            num_strikes=num_strikes,
            min_otm_per_side=cfg.min_otm_per_side,
        )
        ttl = ts - now_ts
        if (
            asset == Asset.ETH
            and _CSP_ROLLING_MIN_SECONDS <= ttl <= _CSP_ROLLING_MAX_SECONDS
        ):
            discounted = (
                Decimal(str(spot))
                * Decimal(10_000 - _CSP_STRIKE_OTM_BPS)
                / Decimal(10_000)
            )
            ticks = (discounted / Decimal(_CSP_STRIKE_TICK_USD)).to_integral_value(
                rounding=ROUND_FLOOR
            )
            csp_strike = float(ticks * Decimal(_CSP_STRIKE_TICK_USD))
            strikes = sorted(set((*strikes, csp_strike)))
        for K in strikes:
            for opt_type in (OptionType.CALL, OptionType.PUT):
                specs.append(
                    OTokenSpec(
                        option_type=opt_type,
                        strike=K,
                        expiry_ts=ts,
                    )
                )
        if (
            asset == Asset.ETH
            and iv is not None
            and _COVERED_CALL_ROLLING_MIN_SECONDS
            <= ttl
            <= _COVERED_CALL_ROLLING_MAX_SECONDS
        ):
            call_strike = covered_call_target_strike(
                spot=spot,
                ttl_seconds=ttl,
                iv=iv,
                risk_free_rate=risk_free_rate,
            )
            if call_strike not in strikes:
                specs.append(
                    OTokenSpec(
                        option_type=OptionType.CALL,
                        strike=call_strike,
                        expiry_ts=ts,
                    )
                )

    return specs
