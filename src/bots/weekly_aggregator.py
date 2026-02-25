"""
Weekly Aggregator Bot

Runs at the configured day/hour (default: Friday 12:00 UTC).
Aggregates all testnet activity for the week:
  1. Query order_events for positions opened this week
  2. Fetch ETH price history for the week
  3. For each user: compute simulated premium, check assignments, compute P&L
  4. Upsert rows into user_weekly_results
  5. Aggregate into weekly_reports with narrative_data JSON
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.db.database import get_client
from src.pricing.historical import get_eth_price_history

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = 60  # seconds


def _week_boundaries() -> tuple[datetime, datetime]:
    """Return (prev Friday 08:00 UTC, this Friday 08:00 UTC) for the expiry cycle that just ended.

    b1nary options expire Friday 08:00 UTC, so the "week" is Friday-to-Friday.
    This runs on Friday after 12:00 UTC, so "this Friday" = today.
    """
    now = datetime.now(timezone.utc)
    days_since_friday = (now.weekday() - 4) % 7
    this_friday = (now - timedelta(days=days_since_friday)).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    if this_friday > now:
        this_friday -= timedelta(days=7)
    prev_friday = this_friday - timedelta(days=7)
    return prev_friday, this_friday


def _get_week_positions(week_start: datetime, week_end: datetime) -> list[dict]:
    """Get all order_events created during [week_start, week_end)."""
    client = get_client()
    result = (
        client.table("order_events")
        .select("*")
        .gte("indexed_at", week_start.isoformat())
        .lt("indexed_at", week_end.isoformat())
        .execute()
    )
    return result.data or []


def _group_by_user(positions: list[dict]) -> dict[str, list[dict]]:
    """Group positions by user_address, dropping entries without a valid address."""
    grouped: dict[str, list[dict]] = {}
    for pos in positions:
        addr = pos.get("user_address", "").lower()
        if addr:
            grouped.setdefault(addr, []).append(pos)
        else:
            logger.warning("Dropping position with missing user_address: %s", pos.get("id", "unknown"))
    return grouped


def _compute_user_week(
    user_addr: str,
    positions: list[dict],
    eth_close: float,
    prev_cumulative: float,
) -> dict:
    """Compute a user's weekly aggregated results."""
    total_premium = 0.0
    assignments = 0

    for pos in positions:
        net = pos.get("net_premium") or pos.get("premium")
        if net is not None:
            try:
                total_premium += float(net) / 1e6  # premium stored in USDC (6 decimals)
            except (ValueError, TypeError):
                logger.warning("Could not parse premium for position %s: %s", pos.get("id", "unknown"), net)

        if pos.get("is_settled") and pos.get("is_itm"):
            assignments += 1

    pnl = total_premium
    for pos in positions:
        if pos.get("is_settled") and pos.get("is_itm"):
            try:
                strike = float(pos["strike_price"]) / 1e8
                amount = float(pos["amount"]) / 1e8
                if pos.get("is_put"):
                    loss = (strike - eth_close) * amount
                else:
                    loss = (eth_close - strike) * amount
                if loss > 0:
                    pnl -= loss
            except (ValueError, TypeError, KeyError):
                logger.warning("Could not compute assignment loss for position %s", pos.get("id", "unknown"))

    cumulative = prev_cumulative + pnl

    return {
        "user_address": user_addr,
        "positions_opened": len(positions),
        "total_simulated_premium": round(total_premium, 4),
        "assignments": assignments,
        "simulated_pnl": round(pnl, 4),
        "cumulative_pnl": round(cumulative, 4),
    }


def _get_prev_cumulative(user_addr: str) -> float:
    """Get the most recent cumulative_pnl for a user, or 0."""
    client = get_client()
    result = (
        client.table("user_weekly_results")
        .select("cumulative_pnl")
        .eq("user_address", user_addr)
        .order("week_start", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return float(result.data[0].get("cumulative_pnl", 0))
    return 0.0


def _build_narrative(
    user_results: list[dict],
    eth_open: float,
    eth_close: float,
) -> dict:
    """Build narrative_data JSON for the weekly report."""
    if not user_results:
        return {}

    highest_premium = max(user_results, key=lambda r: r["total_simulated_premium"])
    most_positions = max(user_results, key=lambda r: r["positions_opened"])

    narrative = {
        "highest_premium_earned": highest_premium["total_simulated_premium"],
        "most_active_positions": most_positions["positions_opened"],
        "total_unique_users": len(user_results),
        "eth_week_change_pct": round(
            (eth_close - eth_open) / eth_open * 100 if eth_open > 0 else 0, 2,
        ),
    }

    assigned_count = sum(1 for r in user_results if r["assignments"] > 0)
    narrative["users_with_assignments"] = assigned_count

    return narrative


async def aggregate_once():
    """Single aggregation cycle."""
    week_start, week_end = _week_boundaries()
    week_start_str = week_start.strftime("%Y-%m-%d")
    week_end_str = week_end.strftime("%Y-%m-%d")

    logger.info("Aggregating week %s → %s", week_start_str, week_end_str)

    positions = _get_week_positions(week_start, week_end)
    if not positions:
        logger.info("No positions this week, skipping aggregation")
        return

    try:
        history = await get_eth_price_history(
            start_ts=week_start.timestamp(),
            end_ts=week_end.timestamp(),
        )
    except Exception:
        logger.exception("Failed to fetch ETH price history for aggregation")
        raise

    eth_open = history[0].price if history else 0
    eth_close = history[-1].price if history else 0
    eth_high = max(p.price for p in history) if history else 0
    eth_low = min(p.price for p in history) if history else 0

    grouped = _group_by_user(positions)
    user_results = []

    for user_addr, user_positions in grouped.items():
        prev_cumulative = _get_prev_cumulative(user_addr)
        result = _compute_user_week(user_addr, user_positions, eth_close, prev_cumulative)
        result["week_start"] = week_start_str
        result["week_end"] = week_end_str
        user_results.append(result)

    client = get_client()
    upsert_failures = 0
    for result in user_results:
        try:
            client.table("user_weekly_results").upsert(
                result, on_conflict="user_address,week_start",
            ).execute()
        except Exception:
            upsert_failures += 1
            logger.exception("Failed to upsert user_weekly_results for %s", result["user_address"])

    if upsert_failures:
        raise RuntimeError(
            f"Failed to upsert {upsert_failures}/{len(user_results)} user weekly results"
        )

    narrative = _build_narrative(user_results, eth_open, eth_close)
    report = {
        "week_start": week_start_str,
        "week_end": week_end_str,
        "total_users": len(grouped),
        "total_positions": len(positions),
        "total_simulated_premium": round(sum(r["total_simulated_premium"] for r in user_results), 4),
        "total_assignments": sum(r["assignments"] for r in user_results),
        "eth_open": round(eth_open, 2),
        "eth_close": round(eth_close, 2),
        "eth_high": round(eth_high, 2),
        "eth_low": round(eth_low, 2),
        "narrative_data": narrative,
    }

    try:
        client.table("weekly_reports").upsert(
            report, on_conflict="week_start",
        ).execute()
        logger.info("Weekly report saved: %d users, %d positions", len(grouped), len(positions))
    except Exception:
        logger.exception("Failed to upsert weekly_reports")
        raise


async def _wait_until_target():
    """Sleep until the next aggregation target (configured day/hour)."""
    now = datetime.now(timezone.utc)

    days_ahead = (settings.weekly_aggregation_day - now.weekday()) % 7
    if days_ahead == 0:
        target = now.replace(
            hour=settings.weekly_aggregation_hour_utc,
            minute=0, second=0, microsecond=0,
        )
        if target <= now:
            days_ahead = 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=settings.weekly_aggregation_hour_utc,
        minute=0, second=0, microsecond=0,
    )

    wait_seconds = (target - now).total_seconds()
    logger.info("Weekly aggregator waiting %.0fs until %s", wait_seconds, target.isoformat())
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
                logger.exception("Weekly aggregation failed (attempt %d/%d)", attempt, _MAX_RETRIES)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF * attempt)
