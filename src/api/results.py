import asyncio
import logging
import re
import time

from fastapi import APIRouter, HTTPException, Query

from src.db.database import get_client
from src.models.simulate import (
    EarningsSnapshot,
    SimulateResponse,
    UserStats,
    UserWeeklyResult,
    WeeklyReport,
)
from src.pricing.deribit import get_eth_iv
from src.pricing.historical import get_eth_price_history
from src.pricing.simulator import simulate_pnl

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
logger = logging.getLogger(__name__)

router = APIRouter()

# --- Cache for simulate (bounded, with eviction) ---
_SIM_TTL = 300  # 5 minutes
_SIM_MAX_SIZE = 256
_sim_cache: dict[tuple, tuple[float, SimulateResponse]] = {}

# --- Cache for weekly report ---
_WEEKLY_TTL = 300
_weekly_cache: WeeklyReport | None = None
_weekly_cache_ts: float = 0.0


@router.get(
    "/prices/simulate",
    response_model=SimulateResponse,
    tags=["Simulation"],
    summary="Simulate a cash-secured put",
)
async def simulate(
    strike: float = Query(
        gt=0, description="Strike price in USD (rounded to nearest $50)"
    ),
    side: str = Query(
        default="buy",
        pattern="^buy$",
        description="Side — currently only 'buy' (reserved for future expansion)",
    ),
):
    """Back-test selling a 7-day cash-secured put at the given strike using
    real ETH price history from CoinGecko and current Deribit IV.

    Returns the premium earned, whether assignment occurred, and a comparison
    against buy-and-hold, staking, and DCA strategies over the same period.
    """
    # Round strike to nearest $50 (minimum $50)
    rounded_strike = max(50, round(strike / 50) * 50)
    cache_key = (rounded_strike,)
    now = time.monotonic()

    cached = _sim_cache.get(cache_key)
    if cached and (now - cached[0]) < _SIM_TTL:
        return cached[1]

    try:
        history, iv = await asyncio.gather(
            get_eth_price_history(),
            get_eth_iv(),
        )
    except Exception:
        logger.exception("Failed to fetch market data for simulation")
        raise HTTPException(502, "Market data unavailable")

    try:
        result = simulate_pnl(strike=rounded_strike, spot_history=history, iv=iv)
    except ValueError as e:
        raise HTTPException(422, f"Simulation failed: {e}")

    # Evict stale entries before inserting
    stale_keys = [k for k, (ts, _) in _sim_cache.items() if (now - ts) >= _SIM_TTL]
    for k in stale_keys:
        del _sim_cache[k]
    if len(_sim_cache) >= _SIM_MAX_SIZE:
        oldest_key = min(_sim_cache, key=lambda k: _sim_cache[k][0])
        del _sim_cache[oldest_key]

    _sim_cache[cache_key] = (time.monotonic(), result)
    return result


@router.get(
    "/results/weekly",
    response_model=WeeklyReport | None,
    tags=["Results"],
    summary="Get latest weekly report",
)
async def get_weekly_report():
    """Return the most recent platform-wide weekly report.

    Includes aggregate stats (total users, positions, premium, assignments)
    and ETH price data for the week. Returns `null` if no report exists yet.
    """
    global _weekly_cache, _weekly_cache_ts

    now = time.monotonic()
    if _weekly_cache is not None and (now - _weekly_cache_ts) < _WEEKLY_TTL:
        return _weekly_cache

    try:
        client = get_client()
        result = (
            client.table("weekly_reports")
            .select("*")
            .order("week_start", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch weekly report")
        raise HTTPException(502, "Could not fetch weekly report")

    if not result.data:
        return None

    row = result.data[0]
    report = WeeklyReport(
        week_start=row["week_start"],
        week_end=row["week_end"],
        total_users=row.get("total_users", 0),
        total_positions=row.get("total_positions", 0),
        total_simulated_premium=row.get("total_simulated_premium", 0),
        total_assignments=row.get("total_assignments", 0),
        eth_open=row.get("eth_open", 0),
        eth_close=row.get("eth_close", 0),
        eth_high=row.get("eth_high", 0),
        eth_low=row.get("eth_low", 0),
        narrative_data=row.get("narrative_data", {}),
    )

    _weekly_cache = report
    _weekly_cache_ts = time.monotonic()
    return report


@router.get(
    "/results/weekly/{address}",
    response_model=UserWeeklyResult | None,
    tags=["Results"],
    summary="Get user's weekly result",
)
async def get_user_weekly(address: str):
    """Return the latest weekly performance result for a single user.

    Designed for shareable result cards. Returns `null` if the user has no
    weekly results yet.
    """
    if not ETH_ADDRESS_RE.match(address):
        raise HTTPException(400, "Invalid Ethereum address")

    try:
        client = get_client()
        result = (
            client.table("user_weekly_results")
            .select("*")
            .eq("user_address", address.lower())
            .order("week_start", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch weekly result for %s", address)
        raise HTTPException(502, "Could not fetch user weekly result")

    if not result.data:
        return None

    row = result.data[0]
    return UserWeeklyResult(
        user_address=row["user_address"],
        week_start=row["week_start"],
        week_end=row["week_end"],
        positions_opened=row.get("positions_opened", 0),
        total_simulated_premium=row.get("total_simulated_premium", 0),
        assignments=row.get("assignments", 0),
        simulated_pnl=row.get("simulated_pnl", 0),
        cumulative_pnl=row.get("cumulative_pnl", 0),
    )


@router.get(
    "/results/stats/{address}",
    response_model=UserStats | None,
    tags=["Results"],
    summary="Get user's cumulative stats",
)
async def get_user_stats(address: str):
    """Return the cumulative track record for a user across all weeks.

    Includes total premium earned, total assignments, best week, and
    cumulative P&L. Returns `null` if the user has no history.
    """
    if not ETH_ADDRESS_RE.match(address):
        raise HTTPException(400, "Invalid Ethereum address")

    try:
        client = get_client()
        result = (
            client.table("user_weekly_results")
            .select("*")
            .eq("user_address", address.lower())
            .order("week_start", desc=True)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch stats for %s", address)
        raise HTTPException(502, "Could not fetch user stats")

    if not result.data:
        return None

    rows = result.data
    return UserStats(
        user_address=address.lower(),
        weeks_active=len(rows),
        cumulative_pnl=rows[0].get("cumulative_pnl", 0),  # latest row has running total
        best_week_pnl=max(r.get("simulated_pnl", 0) for r in rows),
        total_premium_earned=sum(r.get("total_simulated_premium", 0) for r in rows),
        total_assignments=sum(r.get("assignments", 0) for r in rows),
        total_positions=sum(r.get("positions_opened", 0) for r in rows),
    )


@router.get(
    "/results/history/{address}",
    response_model=list[EarningsSnapshot],
    tags=["Results"],
    summary="Get user's weekly earnings history",
)
async def get_earnings_history(address: str):
    """Return all weekly earnings snapshots for a user, sorted chronologically.

    Each entry represents one week of activity with premium earned, assignments,
    weekly P&L, and cumulative P&L. Used by the frontend earnings chart.
    Returns an empty array if the user has no history.
    """
    if not ETH_ADDRESS_RE.match(address):
        raise HTTPException(400, "Invalid Ethereum address")

    try:
        client = get_client()
        result = (
            client.table("user_weekly_results")
            .select(
                "week_start,week_end,total_simulated_premium,assignments,simulated_pnl,cumulative_pnl"
            )
            .eq("user_address", address.lower())
            .order("week_start", desc=False)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch earnings history for %s", address)
        raise HTTPException(502, "Could not fetch earnings history")

    return [
        EarningsSnapshot(
            week_start=row["week_start"],
            week_end=row["week_end"],
            premium_earned=row.get("total_simulated_premium", 0),
            assignments=row.get("assignments", 0),
            pnl=row.get("simulated_pnl", 0),
            cumulative_pnl=row.get("cumulative_pnl", 0),
        )
        for row in (result.data or [])
    ]
