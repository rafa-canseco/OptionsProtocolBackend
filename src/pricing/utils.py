"""Shared pricing utilities.

Used by the otoken_manager bot for on-chain oToken creation.
"""

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from src.config import settings

STRIKE_DECIMALS = 8
FRIDAY_WEEKDAY = 4  # Monday=0, Friday=4
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


def parse_custom_expiry_timestamps(raw: str | None = None) -> list[int] | None:
    """Parse CUSTOM_EXPIRY_TIMESTAMPS into integer timestamps.

    Returns None when unset or malformed so callers can fall back to the
    dynamic policy. This keeps the env var as an emergency override without
    making it the normal production scheduler.
    """
    value = settings.custom_expiry_timestamps if raw is None else raw
    value = value.strip()
    if not value:
        return None
    try:
        timestamps = [int(t.strip()) for t in value.split(",") if t.strip()]
    except ValueError:
        return None
    return timestamps or None


def _normalise_key_part(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip().lower()
    if not text:
        return None
    return "".join(ch if ch.isalnum() else "_" for ch in text).upper()


def _configured_tenors(
    asset: Any = None,
    chain: Any = None,
    product: str | None = "options",
) -> set[str]:
    parts = {
        "asset": _normalise_key_part(asset),
        "chain": _normalise_key_part(chain),
        "product": _normalise_key_part(product),
    }
    env_names = []
    if parts["product"] and parts["chain"] and parts["asset"]:
        env_names.append(
            f"TARGET_EXPIRY_TENORS_{parts['product']}_{parts['chain']}_{parts['asset']}"
        )
    if parts["chain"] and parts["asset"]:
        env_names.append(f"TARGET_EXPIRY_TENORS_{parts['chain']}_{parts['asset']}")
    if parts["asset"]:
        env_names.append(f"TARGET_EXPIRY_TENORS_{parts['asset']}")
    if parts["product"]:
        env_names.append(f"TARGET_EXPIRY_TENORS_{parts['product']}")
    env_names.append("TARGET_EXPIRY_TENORS")

    raw = next((os.getenv(name) for name in env_names if os.getenv(name)), None)
    raw = raw if raw is not None else settings.target_expiry_tenors
    tenors = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if settings.weekly_expiries_enabled:
        tenors.add("weekly")
    return tenors


def get_target_expiries(
    asset: Any = None,
    chain: Any = None,
    product: str | None = "options",
    now: datetime | None = None,
) -> list[int]:
    """Return active target expiries for oToken creation and MM market surface.

    Defaults are intentionally short-dated: 1D and 2D only. Weekly expiries are
    opt-in via TARGET_EXPIRY_TENORS or WEEKLY_EXPIRIES_ENABLED; biweekly/15D is
    excluded unless explicitly configured.
    """
    custom = parse_custom_expiry_timestamps()
    if custom is not None:
        return custom

    if now is None:
        now = datetime.now(timezone.utc)

    short_cutoff = now + timedelta(hours=settings.short_expiry_cutoff_hours)
    standard_cutoff = now + timedelta(hours=settings.expiry_cutoff_hours)
    tenors = _configured_tenors(asset=asset, chain=chain, product=product)

    expiries: set[datetime] = set()
    exp_1d = _next_0800_utc(short_cutoff)
    if "1d" in tenors or "daily" in tenors:
        expiries.add(exp_1d)
    if "2d" in tenors or "daily2" in tenors:
        expiries.add(exp_1d + timedelta(days=1))
    if "weekly" in tenors or "7d" in tenors:
        expiries.add(_next_friday_8am(standard_cutoff))
    if "biweekly" in tenors or "14d" in tenors or "15d" in tenors:
        expiries.add(_next_friday_8am(standard_cutoff) + timedelta(weeks=1))

    cutoff_ts = int(short_cutoff.timestamp())
    result = sorted(int(exp.timestamp()) for exp in expiries)
    return [ts for ts in result if ts > cutoff_ts]


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


def collateral_to_usd(
    row: dict, eth_spot: float, btc_spot: float
) -> float:
    """Convert collateral to USD based on option type and asset."""
    collateral = int(row.get("collateral") or 0)
    is_put = row.get("is_put")
    asset = row.get("asset") or "eth"

    if is_put is True or is_put is None:
        return collateral / 1_000_000
    if asset == "btc":
        return (collateral / 1e8) * btc_spot
    return (collateral / 1e18) * eth_spot
