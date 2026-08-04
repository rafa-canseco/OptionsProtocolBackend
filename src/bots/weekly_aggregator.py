"""Opt-in legacy weekly report materialization.

The default v2 process does not import or schedule this module. When the legacy
rollback is explicitly enabled, each non-empty week uses one fixed-cardinality
source preflight and one atomic database RPC; no source rows or per-wallet writes
cross PostgREST.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import settings
from src.db.database import get_client
from src.pricing.historical import get_eth_price_history

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = 60  # seconds


def _week_boundaries() -> tuple[datetime, datetime]:
    """Return the latest completed Friday-08:00 UTC seven-day window."""
    now = datetime.now(timezone.utc)
    days_since_friday = (now.weekday() - 4) % 7
    this_friday = (now - timedelta(days=days_since_friday)).replace(
        hour=8,
        minute=0,
        second=0,
        microsecond=0,
    )
    if this_friday > now:
        this_friday -= timedelta(days=7)
    return this_friday - timedelta(days=7), this_friday


def _rpc(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = get_client().rpc(name, payload).execute()
    data = response.data
    if not isinstance(data, dict):
        raise RuntimeError(f"{name} returned an invalid fixed-cardinality response")
    return data


def _source_preflight(week_start: datetime, week_end: datetime) -> dict[str, Any]:
    return _rpc(
        "b1nary_legacy_week_source",
        {
            "p_week_start": week_start.isoformat(),
            "p_week_end": week_end.isoformat(),
        },
    )


def _write_week(
    week_start: datetime,
    week_end: datetime,
    *,
    eth_open: float,
    eth_close: float,
    eth_high: float,
    eth_low: float,
) -> dict[str, Any]:
    return _rpc(
        "b1nary_aggregate_legacy_week",
        {
            "p_week_start": week_start.isoformat(),
            "p_week_end": week_end.isoformat(),
            "p_eth_open": eth_open,
            "p_eth_close": eth_close,
            "p_eth_high": eth_high,
            "p_eth_low": eth_low,
        },
    )


async def aggregate_once() -> dict[str, Any] | None:
    """Materialize the latest completed week with a constant two-RPC maximum."""
    week_start, week_end = _week_boundaries()
    week_start_str = week_start.strftime("%Y-%m-%d")
    week_end_str = week_end.strftime("%Y-%m-%d")
    logger.info("Aggregating week %s → %s", week_start_str, week_end_str)

    preflight = _source_preflight(week_start, week_end)
    if preflight.get("has_rows") is not True:
        logger.info("No positions this week, skipping aggregation")
        return None

    try:
        history = await get_eth_price_history(
            start_ts=week_start.timestamp(),
            end_ts=week_end.timestamp(),
        )
    except Exception:
        logger.exception("Failed to fetch ETH price history for aggregation")
        raise

    if not history:
        raise RuntimeError("ETH price history was empty")

    result = _write_week(
        week_start,
        week_end,
        eth_open=history[0].price,
        eth_close=history[-1].price,
        eth_high=max(point.price for point in history),
        eth_low=min(point.price for point in history),
    )
    logger.info(
        "Weekly report saved: %s users, %s positions",
        result.get("wallet_rows"),
        result.get("source_rows"),
    )
    return result


async def _wait_until_target():
    """Sleep until the next aggregation target (configured day/hour)."""
    now = datetime.now(timezone.utc)

    days_ahead = (settings.weekly_aggregation_day - now.weekday()) % 7
    if days_ahead == 0:
        target = now.replace(
            hour=settings.weekly_aggregation_hour_utc,
            minute=0,
            second=0,
            microsecond=0,
        )
        if target <= now:
            days_ahead = 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=settings.weekly_aggregation_hour_utc,
        minute=0,
        second=0,
        microsecond=0,
    )

    wait_seconds = (target - now).total_seconds()
    logger.info(
        "Weekly aggregator waiting %.0fs until %s", wait_seconds, target.isoformat()
    )
    await asyncio.sleep(wait_seconds)


async def run():
    """Main loop: wait for target time, aggregate, retry on failure."""
    logger.info("Weekly aggregator starting")
    while True:
        await _wait_until_target()
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                await aggregate_once()
                break
            except Exception:
                logger.exception(
                    "Weekly aggregation failed (attempt %d/%d)", attempt, _MAX_RETRIES
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF * attempt)
