import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from src.db.database import get_client

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

logger = logging.getLogger(__name__)

router = APIRouter()

# Competition defaults: 2026-03-30 00:00 UTC → 2026-04-12 23:59:59 UTC
_DEFAULT_START = 1774828800
_DEFAULT_END = 1776038399

# Fixed week boundaries
_WEEK1_START = datetime(2026, 3, 30, 0, 0, 0, tzinfo=timezone.utc)
_WEEK1_END = datetime(2026, 4, 5, 23, 59, 59, tzinfo=timezone.utc)
_WEEK2_START = datetime(2026, 4, 6, 0, 0, 0, tzinfo=timezone.utc)
_WEEK2_END = datetime(2026, 4, 12, 23, 59, 59, tzinfo=timezone.utc)

_USDC_DECIMALS = 1_000_000
_MIN_COLLATERAL_USD = 500.0
_BONUS_MULTIPLIER = 1.5
_WHEEL_WINDOW_HOURS = 24
_MAX_RANGE_SECS = 90 * 24 * 3600  # 90-day query cap


def _parse_dt(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp from Supabase into an aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.warning("Could not parse timestamp: %s", ts)
        return None


def _premium_human(row: dict) -> float:
    """Return net premium in USDC. Falls back to gross premium for old rows."""
    raw = row.get("net_premium")
    if raw is None:
        raw = row.get("premium")
    if raw is None:
        return 0.0
    return int(raw) / _USDC_DECIMALS


def _compute_active_days(rows: list[dict], start: int) -> int:
    """Count distinct UTC days on which the wallet had an active position.

    A day D is active if any position has expiry > start_of_day_D UTC, and
    D >= the day the position was indexed.
    """
    covered: set = set()
    for row in rows:
        indexed_dt = _parse_dt(row.get("indexed_at"))
        if indexed_dt is None:
            continue
        indexed_day = indexed_dt.date()

        expiry_ts = row.get("expiry")
        if expiry_ts is None:
            covered.add(indexed_day)
            continue

        try:
            expiry_dt = datetime.fromtimestamp(int(expiry_ts), tz=timezone.utc)
        except (ValueError, OSError):
            covered.add(indexed_day)
            continue
        expiry_date = expiry_dt.date()

        # Walk from indexed_day up to and including expiry_date, since
        # expiry > start_of_that_day means the position is active that day.
        current = indexed_day
        while current <= expiry_date:
            covered.add(current)
            current += timedelta(days=1)

    return len(covered)


def _detect_wheels(rows: list[dict]) -> dict[str, bool]:
    """Detect completed Wheel cycles and return position ids that earn the bonus.

    A Wheel cycle requires both assignments to complete:
      1. A position expires ITM (assigned) — leg 1.
      2. The wallet opens the opposite side (same asset, flipped is_put) within
         24 h of leg 1's settled_at — leg 2.
      3. Leg 2 also expires ITM (assigned again).

    Both legs receive the 1.5x premium bonus. Each position id can appear in
    at most one cycle.
    """
    wheel_ids: dict[str, bool] = {}
    itm_settled = [
        r for r in rows if r.get("is_itm") is True and r.get("settled_at") is not None
    ]
    used_ids: set = set()

    for itm in itm_settled:
        itm_id = itm.get("id")
        if itm_id is None or itm_id in used_ids:
            continue
        settled_dt = _parse_dt(itm.get("settled_at"))
        if settled_dt is None:
            continue
        window_end = settled_dt + timedelta(hours=_WHEEL_WINDOW_HOURS)

        for follow in itm_settled:  # follow must also be ITM settled
            follow_id = follow.get("id")
            if follow_id is None or follow_id in used_ids or follow_id == itm_id:
                continue
            if follow.get("asset") != itm.get("asset"):
                continue
            if follow.get("is_put") == itm.get("is_put"):
                continue
            indexed_dt = _parse_dt(follow.get("indexed_at"))
            if indexed_dt is None:
                continue
            if settled_dt <= indexed_dt <= window_end:
                used_ids.add(itm_id)
                used_ids.add(follow_id)
                wheel_ids[itm_id] = True
                wheel_ids[follow_id] = True
                break

    return wheel_ids


def _detect_perfect_week(
    rows: list[dict],
    wheel_ids: dict[str, bool],
    week_start: datetime,
    week_end: datetime,
) -> dict[str, bool]:
    """Return ids of positions that earn Perfect Week bonus for one week.

    Conditions: no ITM settlement in the week, and position is OTM-settled
    within the week, and position does not already have wheel_bonus.
    """
    bonus_ids: dict[str, bool] = {}
    has_itm_this_week = any(
        r.get("is_itm") is True and _is_settled_in_window(r, week_start, week_end)
        for r in rows
    )
    if has_itm_this_week:
        return bonus_ids

    for row in rows:
        row_id = row.get("id")
        if row_id in wheel_ids:
            continue
        if row.get("is_itm") is not False:
            continue
        if _is_settled_in_window(row, week_start, week_end):
            bonus_ids[row_id] = True

    return bonus_ids


def _is_settled_in_window(row: dict, start: datetime, end: datetime) -> bool:
    """Return True if row's settled_at falls within [start, end]."""
    dt = _parse_dt(row.get("settled_at"))
    if dt is None:
        return False
    return start <= dt <= end


def _compute_wallet_stats(rows: list[dict], start: int) -> dict:
    """Compute all per-wallet statistics needed for both tracks."""
    total_collateral_usd = sum(float(r.get("collateral_usd") or 0.0) for r in rows)
    active_days = _compute_active_days(rows, start)

    wheel_ids = _detect_wheels(rows)
    pw1_ids = _detect_perfect_week(rows, wheel_ids, _WEEK1_START, _WEEK1_END)
    pw2_ids = _detect_perfect_week(rows, wheel_ids, _WEEK2_START, _WEEK2_END)
    bonus_ids = {**wheel_ids, **pw1_ids, **pw2_ids}

    wheel_count = len(wheel_ids) // 2

    adjusted_premium = sum(
        _premium_human(r) * (_BONUS_MULTIPLIER if bonus_ids.get(r.get("id")) else 1.0)
        for r in rows
    )
    earning_rate = (
        round(adjusted_premium / total_collateral_usd, 6)
        if total_collateral_usd > 0
        else None
    )

    settled = [r for r in rows if r.get("settled_at") is not None]
    streak = max_streak = 0
    _dt_min = datetime.min.replace(tzinfo=timezone.utc)
    for pos in sorted(settled, key=lambda r: _parse_dt(r.get("settled_at")) or _dt_min):
        if pos.get("is_itm") is False:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "total_collateral_usd": total_collateral_usd,
        "active_days": active_days,
        "wheel_count": wheel_count,
        "adjusted_premium": adjusted_premium,
        "earning_rate": earning_rate,
        "otm_streak": max_streak,
        "position_count": len(rows),
    }


def _current_week() -> int:
    """Return 1 or 2 based on current UTC time."""
    now = datetime.now(tz=timezone.utc)
    return 1 if now < _WEEK2_START else 2


def _qualification(stats: dict) -> dict:
    """Return qualified flag and progress fields for a wallet."""
    collateral = stats["total_collateral_usd"]
    qualified = collateral >= _MIN_COLLATERAL_USD
    return {
        "qualified": qualified,
        "progress": {
            "collateral_pct": round(min(collateral / _MIN_COLLATERAL_USD, 1.0), 4),
        },
    }


def _build_track1(wallet_stats: dict[str, dict]) -> list[dict]:
    """Build Track 1 (earning rate) rankings.

    Qualified wallets are ranked 1, 2, 3… Non-qualified wallets have rank=null
    and appear after qualified ones, sorted by earning_rate desc.
    """
    qualified = [
        (addr, stats)
        for addr, stats in wallet_stats.items()
        if stats["total_collateral_usd"] >= _MIN_COLLATERAL_USD
    ]
    non_qualified = [
        (addr, stats)
        for addr, stats in wallet_stats.items()
        if not (stats["total_collateral_usd"] >= _MIN_COLLATERAL_USD)
    ]

    def _sort_key(kv: tuple) -> tuple:
        er = kv[1]["earning_rate"]
        return (er if er is not None else -1, kv[1]["total_collateral_usd"])

    qualified_sorted = sorted(qualified, key=_sort_key, reverse=True)
    non_qualified_sorted = sorted(non_qualified, key=_sort_key, reverse=True)

    result = []
    for idx, (addr, stats) in enumerate(qualified_sorted):
        result.append(
            {
                "rank": idx + 1,
                "wallet": addr,
                "earning_rate": stats["earning_rate"],
                "total_earned_usd": round(stats["adjusted_premium"], 6),
                "total_collateral_usd": round(stats["total_collateral_usd"], 2),
                "position_count": stats["position_count"],
                "wheel_count": stats["wheel_count"],
                "active_days": stats["active_days"],
                **_qualification(stats),
            }
        )
    for addr, stats in non_qualified_sorted:
        result.append(
            {
                "rank": None,
                "wallet": addr,
                "earning_rate": stats["earning_rate"],
                "total_earned_usd": round(stats["adjusted_premium"], 6),
                "total_collateral_usd": round(stats["total_collateral_usd"], 2),
                "position_count": stats["position_count"],
                "wheel_count": stats["wheel_count"],
                "active_days": stats["active_days"],
                **_qualification(stats),
            }
        )
    return result


def _build_track2(wallet_stats: dict[str, dict]) -> list[dict]:
    """Build Track 2 (OTM streak) rankings.

    Qualified wallets are ranked 1, 2, 3… Non-qualified wallets have rank=null.
    """
    qualified = [
        (addr, stats)
        for addr, stats in wallet_stats.items()
        if stats["total_collateral_usd"] >= _MIN_COLLATERAL_USD
    ]
    non_qualified = [
        (addr, stats)
        for addr, stats in wallet_stats.items()
        if not (stats["total_collateral_usd"] >= _MIN_COLLATERAL_USD)
    ]

    def _sort_key(kv: tuple) -> tuple:
        er = kv[1]["earning_rate"]
        return (kv[1]["otm_streak"], er if er is not None else -1)

    qualified_sorted = sorted(qualified, key=_sort_key, reverse=True)
    non_qualified_sorted = sorted(non_qualified, key=_sort_key, reverse=True)

    result = []
    for idx, (addr, stats) in enumerate(qualified_sorted):
        result.append(
            {
                "rank": idx + 1,
                "wallet": addr,
                "otm_streak": stats["otm_streak"],
                "position_count": stats["position_count"],
                "earning_rate": stats["earning_rate"],
                **_qualification(stats),
            }
        )
    for addr, stats in non_qualified_sorted:
        result.append(
            {
                "rank": None,
                "wallet": addr,
                "otm_streak": stats["otm_streak"],
                "position_count": stats["position_count"],
                "earning_rate": stats["earning_rate"],
                **_qualification(stats),
            }
        )
    return result


@router.get(
    "/leaderboard", tags=["Leaderboard"], summary="Earnings Challenge leaderboard"
)
async def get_leaderboard(
    start: int = Query(default=_DEFAULT_START),
    end: int = Query(default=_DEFAULT_END),
):
    """Return two-track leaderboard for the Earnings Challenge.

    Track 1 ranks by earning rate (premium / collateral, with bonuses).
    Track 2 ranks by consecutive OTM streak. Only wallets with >= $500
    collateral and >= 8 active days qualify.
    """
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    if end - start > _MAX_RANGE_SECS:
        raise HTTPException(status_code=400, detail="Range must not exceed 90 days")

    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()

    try:
        client = get_client()
        result = (
            client.table("order_events")
            .select(
                "id,user_address,collateral_usd,net_premium,premium,"
                "is_put,asset,indexed_at,expiry,is_itm,settled_at"
            )
            .gte("indexed_at", start_iso)
            .lte("indexed_at", end_iso)
            .limit(10000)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch leaderboard data")
        raise HTTPException(status_code=502, detail="Could not fetch leaderboard data")

    if result.data is None:
        logger.error("Leaderboard DB query returned None")
        raise HTTPException(status_code=502, detail="Could not fetch leaderboard data")
    rows = result.data

    by_wallet: dict[str, list[dict]] = {}
    for row in rows:
        addr = (row.get("user_address") or "").lower()
        if addr:
            by_wallet.setdefault(addr, []).append(row)

    wallet_stats: dict[str, dict] = {}
    for addr, wallet_rows in by_wallet.items():
        try:
            stats = _compute_wallet_stats(wallet_rows, start)
        except Exception:
            logger.exception("Failed to compute stats for wallet %s", addr)
            continue
        wallet_stats[addr] = stats

    qualified_count = sum(
        1
        for s in wallet_stats.values()
        if s["total_collateral_usd"] >= _MIN_COLLATERAL_USD
    )
    total_volume_usd = round(
        sum(s["total_collateral_usd"] for s in wallet_stats.values()), 2
    )
    meta = {
        "competition_start": start,
        "competition_end": end,
        "total_participants": len(wallet_stats),
        "qualified_participants": qualified_count,
        "total_volume_usd": total_volume_usd,
        "current_week": _current_week(),
    }

    return {
        "track1": _build_track1(wallet_stats),
        "track2": _build_track2(wallet_stats),
        "meta": meta,
    }


@router.get(
    "/leaderboard/me",
    tags=["Leaderboard"],
    summary="Personal Earnings Challenge stats (no eligibility filter)",
)
async def get_leaderboard_me(
    address: str = Query(...),
    start: int = Query(default=_DEFAULT_START),
    end: int = Query(default=_DEFAULT_END),
):
    """Return stats for a single wallet regardless of eligibility.

    Includes a `qualifies` field showing whether the wallet meets the
    $500 collateral and 8 active-day thresholds for the leaderboard prize.
    """
    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    if end - start > _MAX_RANGE_SECS:
        raise HTTPException(status_code=400, detail="Range must not exceed 90 days")

    addr = address.lower()
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()

    try:
        client = get_client()
        result = (
            client.table("order_events")
            .select(
                "id,user_address,collateral_usd,net_premium,premium,"
                "is_put,asset,indexed_at,expiry,is_itm,settled_at"
            )
            .eq("user_address", addr)
            .gte("indexed_at", start_iso)
            .lte("indexed_at", end_iso)
            .limit(10000)
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch leaderboard/me data for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch leaderboard data")

    if result.data is None:
        logger.error("leaderboard/me DB query returned None for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch leaderboard data")

    rows = result.data
    if not rows:
        return {
            "wallet": addr,
            "position_count": 0,
            "total_collateral_usd": 0.0,
            "total_earned_usd": 0.0,
            "earning_rate": None,
            "active_days": 0,
            "wheel_count": 0,
            "otm_streak": 0,
            "qualifies": False,
        }

    try:
        stats = _compute_wallet_stats(rows, start)
    except Exception:
        logger.exception("Failed to compute stats for wallet %s", addr)
        raise HTTPException(status_code=502, detail="Could not compute wallet stats")

    qualifies = stats["total_collateral_usd"] >= _MIN_COLLATERAL_USD

    return {
        "wallet": addr,
        "position_count": stats["position_count"],
        "total_collateral_usd": round(stats["total_collateral_usd"], 2),
        "total_earned_usd": round(stats["adjusted_premium"], 6),
        "earning_rate": stats["earning_rate"],
        "active_days": stats["active_days"],
        "wheel_count": stats["wheel_count"],
        "otm_streak": stats["otm_streak"],
        "qualifies": qualifies,
    }
