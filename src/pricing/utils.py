"""Shared pricing utilities.

Pure functions with zero settings dependencies. Used by the
otoken_manager bot for on-chain oToken creation.
"""

from datetime import datetime, timezone, timedelta

STRIKE_DECIMALS = 8
FRIDAY_WEEKDAY = 4  # Monday=0, Friday=4
CUTOFF_HOURS = 48
WEEKLY_COUNT = 2
TARGET_MONTHLY_DAYS = 28
_MIN_VALID_FRIDAYS = 3


def strike_to_8_decimals(strike_usd: float) -> int:
    """Convert a strike price in USD to 8-decimal integer.

    Uses round() to avoid float truncation errors.
    e.g. $2000 -> 200000000000
    """
    return round(strike_usd * 10**STRIKE_DECIMALS)


def _next_friday_8am(after: datetime) -> datetime:
    """Return the first Friday 08:00 UTC strictly after `after`."""
    days_ahead = (FRIDAY_WEEKDAY - after.weekday()) % 7
    if days_ahead == 0:
        friday = after.replace(hour=8, minute=0, second=0, microsecond=0)
        if friday <= after:
            days_ahead = 7
    candidate = after + timedelta(days=days_ahead)
    return candidate.replace(hour=8, minute=0, second=0, microsecond=0)


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

    # Build a pool of upcoming Fridays (8 weeks covers all cases)
    fridays: list[datetime] = []
    candidate = _next_friday_8am(now)
    for _ in range(8):
        fridays.append(candidate)
        candidate = candidate + timedelta(weeks=1)

    # Filter out Fridays within the 48h cutoff
    valid = [f for f in fridays if f > cutoff]

    if len(valid) < _MIN_VALID_FRIDAYS:
        raise ValueError(
            f"Need at least {_MIN_VALID_FRIDAYS} valid Fridays after "
            f"48h cutoff, got {len(valid)}. now={now.isoformat()}"
        )

    # Pick weekly: first 2 valid Fridays
    weekly = valid[:WEEKLY_COUNT]

    # Pick monthly: Friday closest to 28 days out, not already selected
    target = now + timedelta(days=TARGET_MONTHLY_DAYS)
    weekly_set = set(weekly)
    remaining = [f for f in valid if f not in weekly_set]
    monthly = min(
        remaining,
        key=lambda f: abs((f - target).total_seconds()),
    )

    result = sorted({*weekly, monthly})
    return [int(f.timestamp()) for f in result]
