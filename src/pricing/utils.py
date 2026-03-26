"""Shared pricing utilities.

Used by the otoken_manager bot for on-chain oToken creation.
"""

import time
from datetime import datetime, timezone, timedelta

from src.config import settings

STRIKE_DECIMALS = 8
FRIDAY_WEEKDAY = 4  # Monday=0, Friday=4
SHORT_TERM_DAYS = 4
_48H_SECONDS = 48 * 3600


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
    """Return expiry timestamps at 08:00 UTC including a 1-day slot.

    Selection:
      0. Daily (~1d): next 08:00 UTC (after short cutoff)
      1. Short-term (~3d): nearest 08:00 UTC slot (any day)
      2. Weekly (~7d): Friday 08:00 UTC
      3. Biweekly (~14d): Friday 08:00 UTC

    Short-term expiries (TTL <= 48h) use the short cutoff (4h).
    Standard expiries use the standard cutoff (48h).
    All timestamps satisfy ``ts % 86400 == 28800``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    short_cutoff = now + timedelta(hours=settings.short_expiry_cutoff_hours)
    standard_cutoff = now + timedelta(hours=settings.expiry_cutoff_hours)

    # Daily (~1d): next 08:00 UTC after short cutoff
    exp_1d = _next_0800_utc(short_cutoff)

    # Short-term (~3d): nearest 08:00 UTC slot after standard cutoff
    target_3d = now + timedelta(days=SHORT_TERM_DAYS)
    exp_3d = target_3d.replace(hour=8, minute=0, second=0, microsecond=0)
    if exp_3d <= standard_cutoff:
        exp_3d = _next_0800_utc(standard_cutoff)

    # Weekly: first 2 Fridays after standard cutoff
    exp_7d = _next_friday_8am(standard_cutoff)
    exp_14d = exp_7d + timedelta(weeks=1)

    # If 3d lands on the same day as the 7d Friday, shift to next day
    if exp_3d == exp_7d:
        exp_3d = exp_7d + timedelta(days=1)

    # Dedup (1-day may coincide with 3d or Friday slot)
    result = sorted({exp_1d, exp_3d, exp_7d, exp_14d})
    return [int(f.timestamp()) for f in result]


