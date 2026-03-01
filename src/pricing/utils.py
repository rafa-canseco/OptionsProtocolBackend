"""Shared pricing utilities.

Pure functions with zero settings dependencies. Used by the
otoken_manager bot for on-chain oToken creation.
"""

from datetime import datetime, timezone, timedelta

STRIKE_DECIMALS = 8


def strike_to_8_decimals(strike_usd: float) -> int:
    """Convert a strike price in USD to 8-decimal integer.

    Uses round() to avoid float truncation errors.
    e.g. $2000 -> 200000000000
    """
    return round(strike_usd * 10**STRIKE_DECIMALS)


def expiry_days_to_timestamp(days: int) -> int:
    """Convert days-from-now to a UTC 08:00 expiry timestamp.

    The contract requires expiry % 86400 == 28800 (08:00 UTC).
    Anchored to the next 08:00 UTC boundary so that all calls within
    the same 24h window (08:00 to 08:00) produce the same timestamp.
    If days=0 and current time is past 08:00 UTC, returns tomorrow's 08:00.
    """
    now = datetime.now(timezone.utc)
    today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if today_8am <= now:
        base = today_8am + timedelta(days=1)
    else:
        base = today_8am
    expiry_dt = base + timedelta(days=days)
    return int(expiry_dt.timestamp())
