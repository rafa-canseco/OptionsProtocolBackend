import asyncio
import logging
import re
import time

from fastapi import APIRouter, HTTPException, Query

from src.config import settings
from src.db.database import get_client
from src.models.simulate import (
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

# --- Cache for simulate ---
_SIM_TTL = 300  # 5 minutes
_sim_cache: dict[tuple, tuple[float, SimulateResponse]] = {}

# --- Cache for weekly report ---
_WEEKLY_TTL = 300
_weekly_cache: WeeklyReport | None = None
_weekly_cache_ts: float = 0.0


@router.get("/prices/simulate", response_model=SimulateResponse)
async def simulate(
    strike: float = Query(gt=0, description="Strike price in USD"),
    side: str = Query(default="buy", pattern="^buy$", description="Side (buy only for CSP)"),
):
    """Simulate selling a cash-secured put at the given strike over the last 7 days."""
    # Round strike to nearest $50 for cache key
    cache_key = (round(strike / 50) * 50,)
    now = time.monotonic()

    cached = _sim_cache.get(cache_key)
    if cached and (now - cached[0]) < _SIM_TTL:
        return cached[1]

    try:
        history, iv = await asyncio.gather(
            get_eth_price_history(days=7),
            get_eth_iv(),
        )
    except Exception:
        logger.exception("Failed to fetch market data for simulation")
        raise HTTPException(502, "Market data unavailable")

    result = simulate_pnl(strike=strike, spot_history=history, iv=iv)

    _sim_cache[cache_key] = (time.monotonic(), result)
    return result


@router.get("/results/weekly", response_model=WeeklyReport | None)
async def get_weekly_report():
    """Get the latest weekly report."""
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


@router.get("/results/weekly/{address}", response_model=UserWeeklyResult | None)
async def get_user_weekly(address: str):
    """Get a user's latest weekly result for shareable card."""
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
        logger.exception(f"Failed to fetch weekly result for {address}")
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


@router.get("/results/stats/{address}", response_model=UserStats | None)
async def get_user_stats(address: str):
    """Get a user's cumulative track record."""
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
        logger.exception(f"Failed to fetch stats for {address}")
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
