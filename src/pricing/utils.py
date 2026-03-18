"""Shared pricing utilities.

Used by the otoken_manager bot for on-chain oToken creation.
"""

from datetime import datetime, timezone, timedelta

from src.config import settings

STRIKE_DECIMALS = 8
FRIDAY_WEEKDAY = 4  # Monday=0, Friday=4
SHORT_TERM_DAYS = 4


def _cutoff_hours() -> int:
    return settings.expiry_cutoff_hours


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


def _next_0800_utc(after: datetime) -> datetime:
    """Return the first 08:00 UTC strictly after `after`."""
    candidate = after.replace(hour=8, minute=0, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def get_friday_expiries(
    now: datetime | None = None,
) -> list[int]:
    """Return exactly 3 expiry timestamps at 08:00 UTC.

    Selection:
      1. Short-term (~3d): nearest 08:00 UTC slot (any day)
      2. Weekly (~7d): Friday 08:00 UTC
      3. Biweekly (~14d): Friday 08:00 UTC

    Slots within 48h of `now` are excluded so users don't see
    options about to expire. All timestamps satisfy the contract
    constraint ``ts % 86400 == 28800``.

    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now + timedelta(hours=_cutoff_hours())

    # Short-term (~3d): nearest 08:00 UTC slot
    target_3d = now + timedelta(days=SHORT_TERM_DAYS)
    exp_3d = target_3d.replace(hour=8, minute=0, second=0, microsecond=0)
    if exp_3d <= cutoff:
        exp_3d = _next_0800_utc(cutoff)

    # Weekly: first 2 Fridays after cutoff
    exp_7d = _next_friday_8am(cutoff)
    exp_14d = exp_7d + timedelta(weeks=1)

    # If 3d lands on the same day as the 7d Friday, shift to next day
    if exp_3d == exp_7d:
        exp_3d = exp_7d + timedelta(days=1)

    result = sorted({exp_3d, exp_7d, exp_14d})
    return [int(f.timestamp()) for f in result]
