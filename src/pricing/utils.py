"""Shared pricing utilities.

Used by the otoken_manager bot for on-chain oToken creation.
"""

import math
import time
from datetime import datetime, timezone, timedelta

from src.config import settings

STRIKE_DECIMALS = 8
FRIDAY_WEEKDAY = 4  # Monday=0, Friday=4
_48H_SECONDS = 48 * 3600
_BPS = 10_000


def cutoff_hours_for_expiry(expiry_ts: int, now_ts: int | None = None) -> int:
    """Return the cutoff hours for a given expiry based on current TTL.

    If the option expires within 48h (short-term / 1-day), use the
    short cutoff (4h). Otherwise use the standard cutoff (48h).
    """
    if now_ts is None:
        now_ts = int(time.time())
    ttl = expiry_ts - now_ts
    if ttl <= _48H_SECONDS:
        return settings.short_expiry_cutoff_hours
    return settings.expiry_cutoff_hours


def strike_to_8_decimals(strike_usd: float) -> int:
    """Convert a strike price in USD to 8-decimal integer.

    Uses round() to avoid float truncation errors.
    e.g. $2000 -> 200000000000
    """
    return round(strike_usd * 10**STRIKE_DECIMALS)


def get_csp_expiry(
    now: datetime | None = None,
    *,
    min_delay_hours: int = 36,
    max_delay_hours: int = 60,
) -> int:
    """Return the first valid 08:00 UTC expiry inside the CSP policy window."""
    if min_delay_hours <= 0 or max_delay_hours <= min_delay_hours:
        raise ValueError("invalid CSP expiry window")
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    now = now.astimezone(timezone.utc)
    lower = now + timedelta(hours=min_delay_hours)
    upper = now + timedelta(hours=max_delay_hours)
    candidate = lower.replace(hour=8, minute=0, second=0, microsecond=0)
    if candidate < lower:
        candidate += timedelta(days=1)
    if candidate > upper:
        raise ValueError("CSP expiry window contains no 08:00 UTC expiry")
    return int(candidate.timestamp())


def csp_put_strike(
    spot: float,
    *,
    otm_bps: int = 1500,
    tick: float = 25.0,
) -> float:
    """Return a deterministic fixed-moneyness put strike, rounded down."""
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("spot must be positive and finite")
    if otm_bps <= 0 or otm_bps >= _BPS:
        raise ValueError("otm_bps must be between 1 and 9999")
    if not math.isfinite(tick) or tick <= 0:
        raise ValueError("tick must be positive and finite")
    raw_strike = spot * (_BPS - otm_bps) / _BPS
    strike = math.floor(raw_strike / tick) * tick
    if strike <= 0:
        raise ValueError("rounded CSP strike must be positive")
    return float(strike)


def _next_friday_8am(after: datetime) -> datetime:
    """Return the first Friday 08:00 UTC strictly after `after`."""
    days_ahead = (FRIDAY_WEEKDAY - after.weekday()) % 7
    if days_ahead == 0:
        friday = after.replace(hour=8, minute=0, second=0, microsecond=0)
        if friday <= after:
            days_ahead = 7
    candidate = after + timedelta(days=days_ahead)
    return candidate.replace(hour=8, minute=0, second=0, microsecond=0)


def _next_0800_utc(after: datetime) -> datetime:
    """Return the first 08:00 UTC strictly after `after`."""
    candidate = after.replace(hour=8, minute=0, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def get_expiries(
    now: datetime | None = None,
) -> list[int]:
    """Return expiry timestamps at 08:00 UTC.

    Selection:
      0. Daily: next 08:00 UTC (after short cutoff)
      1. Near Friday: first Friday after short cutoff
      2. Weekly: first Friday after standard cutoff
      3. Biweekly: weekly + 7 days

    Dedup via set handles overlap (e.g. near_fri == weekly when no
    Friday falls in the gap, or 1d == near_fri on Thursday night).
    All timestamps satisfy ``ts % 86400 == 28800``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    short_cutoff = now + timedelta(hours=settings.short_expiry_cutoff_hours)
    standard_cutoff = now + timedelta(hours=settings.expiry_cutoff_hours)

    # Daily: next 08:00 UTC after short cutoff
    exp_1d = _next_0800_utc(short_cutoff)

    # Near Friday: first Friday after short cutoff
    exp_near_fri = _next_friday_8am(short_cutoff)

    # Weekly: first 2 Fridays after standard cutoff
    exp_7d = _next_friday_8am(standard_cutoff)
    exp_14d = exp_7d + timedelta(weeks=1)

    result = sorted({exp_1d, exp_near_fri, exp_7d, exp_14d})
    return [int(f.timestamp()) for f in result]


def collateral_to_usd(row: dict, eth_spot: float, btc_spot: float) -> float:
    """Convert collateral to USD based on option type and asset."""
    collateral = int(row.get("collateral") or 0)
    is_put = row.get("is_put")
    asset = row.get("asset") or "eth"

    if is_put is True or is_put is None:
        return collateral / 1_000_000
    if asset == "btc":
        return (collateral / 1e8) * btc_spot
    return (collateral / 1e18) * eth_spot
