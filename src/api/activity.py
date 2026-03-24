import logging
import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException

from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter()

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_USDC_DECIMALS = 1_000_000  # 1e6
_WETH_DECIMALS = 10**18  # 1e18
_CBBTC_DECIMALS = 10**8  # 1e8
_STRIKE_DECIMALS = 10**8  # oToken strike uses 8 decimals

_CALL_DECIMALS = {"eth": _WETH_DECIMALS, "btc": _CBBTC_DECIMALS}


def _collateral_usd(row: dict) -> float:
    """Convert raw collateral to USD value.

    Puts: collateral is USDC → divide by 1e6.
    Calls: collateral is the underlying (WETH/cbBTC) → convert to USD
    via (collateral / asset_decimals) * (strike_price / 1e8).
    """
    raw = int(row.get("collateral") or 0)
    is_put = row.get("is_put")
    if is_put is None or is_put:
        return raw / _USDC_DECIMALS
    asset = row.get("asset") or "eth"
    decimals = _CALL_DECIMALS.get(asset, _WETH_DECIMALS)
    strike = int(row.get("strike_price") or 0)
    native_amount = raw / decimals
    strike_usd = strike / _STRIKE_DECIMALS
    return native_amount * strike_usd


def _premium_human(row: dict) -> float:
    """Return net premium in USDC. Falls back to gross premium for old rows."""
    raw = row.get("net_premium") or row.get("premium") or 0
    return int(raw) / _USDC_DECIMALS


def _parse_date(ts: str | None) -> date | None:
    """Parse an ISO 8601 timestamp string from Supabase into a date object.

    Note: indexed_at is the DB insertion timestamp, not the on-chain block
    timestamp. Active days and daysSinceFirst reflect when events were stored,
    not when blocks were mined. This is acceptable for v1 activity tracking.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        logger.warning("Could not parse timestamp: %s", ts)
        return None


def _compute_metrics(rows: list[dict]) -> dict:
    """Aggregate order_events rows into per-wallet activity metrics."""
    if not rows:
        return {
            "totalVolume": 0.0,
            "totalPremiumEarned": 0.0,
            "positionCount": 0,
            "activeDays": 0,
            "daysSinceFirst": 0,
        }

    total_volume = sum(_collateral_usd(r) for r in rows)
    total_premium = sum(_premium_human(r) for r in rows)
    position_count = len(rows)

    dates = [_parse_date(r.get("indexed_at")) for r in rows]
    dates = [d for d in dates if d is not None]

    active_days = len(set(dates))
    today = datetime.now(tz=timezone.utc).date()
    first_date = min(dates) if dates else today
    days_since_first = (today - first_date).days

    return {
        "totalVolume": round(total_volume, 2),
        "totalPremiumEarned": round(total_premium, 2),
        "positionCount": position_count,
        "activeDays": active_days,
        "daysSinceFirst": days_since_first,
    }


@router.get(
    "/activity/{wallet_address}",
    tags=["Activity"],
    summary="Get per-wallet activity metrics",
)
async def get_activity(wallet_address: str):
    """Return aggregated on-chain activity metrics for a wallet.

    Data is sourced from indexed OrderExecuted events. Returns zeroes for
    wallets with no activity. Metrics are computed on-the-fly from the
    order_events table — no pre-aggregation required.
    """
    if not ETH_ADDRESS_RE.match(wallet_address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")

    try:
        client = get_client()
        result = (
            client.table("order_events")
            .select(
                "collateral,net_premium,premium,is_put,strike_price,asset,indexed_at"
            )
            .eq("user_address", wallet_address.lower())
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch activity for %s", wallet_address)
        raise HTTPException(status_code=502, detail="Could not fetch activity data")

    rows = result.data or []
    metrics = _compute_metrics(rows)
    return {"wallet": wallet_address.lower(), **metrics}
