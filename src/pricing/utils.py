"""Shared pricing utilities.

Pure functions with zero settings dependencies. Used by the
otoken_manager bot for on-chain oToken creation.
"""

import calendar
from datetime import datetime, timezone, timedelta

STRIKE_DECIMALS = 8
FRIDAY_WEEKDAY = 4  # Monday=0, Friday=4
CUTOFF_HOURS = 48
WEEKLY_COUNT = 2
TARGET_MONTHLY_DAYS = 28


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


def _next_friday_8am(after: datetime) -> datetime:
    """Return the first Friday 08:00 UTC strictly after `after`."""
    days_ahead = (FRIDAY_WEEKDAY - after.weekday()) % 7
    if days_ahead == 0:
        friday = after.replace(hour=8, minute=0, second=0, microsecond=0)
        if friday <= after:
            days_ahead = 7
    candidate = after + timedelta(days=days_ahead)
    return candidate.replace(hour=8, minute=0, second=0, microsecond=0)


def _last_friday_of_month(year: int, month: int) -> datetime:
    """Return the last Friday 08:00 UTC of the given month."""
    last_day = calendar.monthrange(year, month)[1]
    dt = datetime(year, month, last_day, 8, 0, 0, tzinfo=timezone.utc)
    days_back = (dt.weekday() - FRIDAY_WEEKDAY) % 7
    return dt - timedelta(days=days_back)


def get_friday_expiries(
    now: datetime | None = None,
) -> list[int]:
    """Return exactly 3 fixed Friday 08:00 UTC expiry timestamps.

    Selection:
      1. Next valid Friday (~1 week out)
      2. 2nd Friday (~2 weeks out)
      3. Friday closest to 28 days out (monthly), not already selected

    Fridays within 48h of `now` are excluded so users don't see
    options about to expire. All timestamps satisfy the contract
    constraint ``ts % 86400 == 28800``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now + timedelta(hours=CUTOFF_HOURS)

    # Build a pool of upcoming Fridays (next 6 weeks covers all cases)
    fridays: list[datetime] = []
    candidate = _next_friday_8am(now)
    for _ in range(8):
        fridays.append(candidate)
        candidate = candidate + timedelta(weeks=1)

    # Filter out Fridays within the 48h cutoff
    valid = [f for f in fridays if f > cutoff]

    # Pick weekly: first 2 valid Fridays
    weekly = valid[:WEEKLY_COUNT]

    # Pick monthly: Friday closest to 28 days out, not already selected
    target = now + timedelta(days=TARGET_MONTHLY_DAYS)
    weekly_set = set(weekly)
    monthly = min(
        (f for f in valid if f not in weekly_set),
        key=lambda f: abs((f - target).total_seconds()),
    )

    result = sorted({*weekly, monthly})
    return [int(f.timestamp()) for f in result]
