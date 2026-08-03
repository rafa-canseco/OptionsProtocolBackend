"""Bounded yield tracking API endpoints."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from web3 import Web3

from src.api.position_pagination import PAGE_DEFAULT, PAGE_MAX, PositionCursorError
from src.api.yield_pagination import build_yield_page, fetch_yield_page
from src.config import settings
from src.contracts.web3_client import get_margin_pool
from src.db.database import get_client
from src.models.yield_api import (
    YieldHistoryResponse,
    YieldPositionsResponse,
    YieldStatsResponse,
    YieldSummaryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ASSET_ADDRESSES = {
    "usdc": settings.usdc_address,
    "eth": settings.weth_address,
    "btc": settings.wbtc_address,
}
_ASSET_DECIMALS = {"usdc": 6, "eth": 18, "btc": 8}
_ACCRUED_CACHE_SECONDS = 15.0
_STATS_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class _AccruedSnapshot:
    values: dict[str, int | None]
    as_of: str
    expires_at: float


@dataclass(frozen=True)
class _StatsSnapshot:
    rows: list[dict[str, Any]] | None
    as_of: str


_accrued_cache: tuple[float, _AccruedSnapshot] | None = None
_stats_cache: tuple[float, _StatsSnapshot] | None = None
_accrued_lock = threading.Lock()
_stats_lock = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(value: datetime | str) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _human(amount: int | None, asset: str) -> float | None:
    """Convert raw token amount to the existing JSON number representation."""
    if amount is None:
        return None
    decimals = _ASSET_DECIMALS.get(asset, 18)
    return float(Decimal(amount) / (Decimal(10) ** decimals))


def _rpc_payload(result: Any, label: str) -> dict[str, Any]:
    payload = result.data
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} RPC returned an invalid payload")
    return payload


def _get_accrued_yield() -> dict[str, int | None]:
    """Read three assets from one MarginPool instance with isolated failures."""
    try:
        pool = get_margin_pool()
    except Exception:
        logger.warning("Failed to initialize MarginPool accrued-yield reader")
        return {asset: None for asset in _ASSET_ADDRESSES}

    accrued: dict[str, int | None] = {}
    for asset_symbol, asset_addr in _ASSET_ADDRESSES.items():
        try:
            checksum = Web3.to_checksum_address(asset_addr)
            value = pool.functions.getAccruedYield(checksum).call()
            accrued[asset_symbol] = int(value)
        except Exception:
            logger.warning("Failed to read accrued yield for %s", asset_symbol)
            accrued[asset_symbol] = None
    return accrued


def _get_accrued_snapshot() -> _AccruedSnapshot:
    global _accrued_cache
    now_mono = time.monotonic()
    cached = _accrued_cache
    if cached is not None and now_mono < cached[0]:
        return cached[1]

    with _accrued_lock:
        observed_mono = time.monotonic()
        cached = _accrued_cache
        if cached is not None and observed_mono < cached[0]:
            return cached[1]

        # The three chain calls are sequential. Timestamp the observation before
        # the first call rather than claiming all values were seen at a later time.
        observed_at = _iso(_utc_now())
        expires_at = observed_mono + _ACCRUED_CACHE_SECONDS
        values = _get_accrued_yield()
        if time.monotonic() >= expires_at:
            raise RuntimeError("Accrued-yield refresh exceeded its maximum age")

        snapshot = _AccruedSnapshot(
            values=values,
            as_of=observed_at,
            expires_at=expires_at,
        )
        _accrued_cache = (expires_at, snapshot)
        return snapshot


def _require_current_accrued(snapshot: _AccruedSnapshot) -> None:
    if time.monotonic() >= snapshot.expires_at:
        raise RuntimeError("Accrued-yield snapshot exceeded its maximum age")


def _get_stats_snapshot(client: Any) -> _StatsSnapshot:
    global _stats_cache
    now_mono = time.monotonic()
    cached = _stats_cache
    if cached is not None and now_mono < cached[0]:
        if cached[1].rows is None:
            raise RuntimeError("Yield stats are temporarily unavailable")
        return cached[1]

    with _stats_lock:
        now_mono = time.monotonic()
        cached = _stats_cache
        if cached is not None and now_mono < cached[0]:
            if cached[1].rows is None:
                raise RuntimeError("Yield stats are temporarily unavailable")
            return cached[1]
        observed_at = _iso(_utc_now())
        try:
            payload = _rpc_payload(
                client.rpc("b1nary_yield_stats", {}).execute(),
                "Yield stats",
            )
            rows = payload.get("rows") or []
            if not isinstance(rows, list):
                raise RuntimeError("Yield stats RPC returned invalid rows")
            snapshot = _StatsSnapshot(
                rows=rows,
                as_of=_iso(payload.get("as_of") or observed_at),
            )
        except Exception:
            snapshot = _StatsSnapshot(rows=None, as_of=observed_at)
        _stats_cache = (now_mono + _STATS_CACHE_SECONDS, snapshot)
        if snapshot.rows is None:
            raise RuntimeError("Yield stats are temporarily unavailable")
        return snapshot


def _estimated_amount(
    accrued: int | None,
    numerator_weight: Any,
    denominator_weight: Any,
    protocol_fee_bps: int,
) -> int:
    if accrued is None or accrued <= 0:
        return 0
    numerator = Decimal(str(numerator_weight or 0))
    denominator = Decimal(str(denominator_weight or 0))
    if numerator <= 0 or denominator <= 0:
        return 0
    distributable = accrued * (10_000 - protocol_fee_bps) // 10_000
    return int(
        (Decimal(distributable) * numerator / denominator).to_integral_value(
            rounding=ROUND_DOWN
        )
    )


def _validate_address(address: str) -> str:
    if not _ETH_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    return address.lower()


@router.get(
    "/yield/user/{address}",
    tags=["Yield"],
    summary="Yield summary per user",
    response_model=YieldSummaryResponse,
)
async def get_yield_summary(address: str):
    """Return fixed-cardinality allocation totals and global pro-rata estimates."""
    addr = _validate_address(address)
    try:
        accrued = _get_accrued_snapshot()
        payload = _rpc_payload(
            get_client()
            .rpc(
                "b1nary_yield_user_summary",
                {
                    "p_user_address": addr,
                    "p_as_of": accrued.as_of,
                    "p_accrued": accrued.values,
                    "p_protocol_fee_bps": settings.protocol_fee_bps,
                },
            )
            .execute(),
            "Yield summary",
        )
        _require_current_accrued(accrued)
    except Exception:
        logger.exception("Failed to fetch yield summary for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch yield data")

    rows = payload.get("rows") or []
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Could not fetch yield data")
    assets = []
    for row in rows:
        asset = str(row["asset"])
        pending = int(row.get("pending_raw") or 0)
        delivered = int(row.get("delivered_raw") or 0)
        # SQL sums the legacy per-position floors. Flooring one aggregate user
        # share is not equivalent when a user owns multiple fractional shares.
        estimated = int(row.get("estimated_accruing_raw") or 0)
        if pending == 0 and delivered == 0 and estimated == 0:
            continue
        total = pending + delivered + estimated
        assets.append(
            {
                "asset": asset,
                "pending_raw": pending,
                "pending": _human(pending, asset),
                "delivered_raw": delivered,
                "delivered": _human(delivered, asset),
                "estimated_accruing_raw": estimated,
                "estimated_accruing": _human(estimated, asset),
                "total_raw": total,
                "total": _human(total, asset),
            }
        )
    return {
        "wallet": addr,
        "assets": assets,
        "as_of": _iso(payload.get("as_of") or accrued.as_of),
        "accrued_as_of": accrued.as_of,
    }


@router.get(
    "/yield/user/{address}/positions",
    tags=["Yield"],
    summary="Positions with estimated accrued yield",
    response_model=YieldPositionsResponse,
)
async def get_yield_positions(
    address: str,
    request: Request,
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    cursor: str | None = None,
):
    """Return a bounded keyset page with server-side global denominators."""
    if "as_of" in request.query_params:
        raise HTTPException(
            status_code=400,
            detail="as_of is controlled by the server and signed continuation cursor",
        )
    addr = _validate_address(address)
    try:
        accrued = _get_accrued_snapshot() if cursor is None else None
        payload = fetch_yield_page(
            get_client(),
            address=addr,
            stream="yield_positions",
            limit=limit,
            cursor=cursor,
            as_of=accrued.as_of if accrued is not None else None,
            accrued=accrued.values if accrued is not None else None,
            protocol_fee_bps=(
                settings.protocol_fee_bps if accrued is not None else None
            ),
        )
        page = build_yield_page(
            payload,
            address=addr,
            stream="yield_positions",
            limit=limit,
        )
        if accrued is not None:
            _require_current_accrued(accrued)
    except PositionCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if "signing key" in str(exc):
            raise HTTPException(
                status_code=503, detail="Yield pagination is unavailable"
            ) from exc
        logger.exception("Failed to fetch yield positions for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch positions")
    except Exception:
        logger.exception("Failed to fetch yield positions for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch positions")

    snapshot_accrued = page["accrued"]
    snapshot_fee_bps = page["protocol_fee_bps"]
    if not isinstance(snapshot_accrued, dict) or not isinstance(snapshot_fee_bps, int):
        raise HTTPException(status_code=502, detail="Could not fetch positions")

    positions = []
    for row in page["rows"]:
        asset = str(row["asset"])
        estimated = _estimated_amount(
            snapshot_accrued.get(asset),
            row.get("position_weight"),
            row.get("global_weight"),
            snapshot_fee_bps,
        )
        positions.append(
            {
                "id": str(row["id"]),
                "vault_id": int(row["vault_id"]),
                "asset": asset,
                "collateral_amount": int(row["collateral_amount"]),
                "deposited_at": _iso(row["deposited_at"]),
                "settled_at": (
                    _iso(row["settled_at"]) if row.get("settled_at") else None
                ),
                "is_active": row.get("settled_at") is None,
                "estimated_yield_raw": estimated,
                "estimated_yield": _human(estimated, asset),
            }
        )
    total_rows = payload.get("totals") or []
    if not isinstance(total_rows, list):
        raise HTTPException(status_code=502, detail="Could not fetch positions")
    totals = []
    for row in total_rows:
        asset = str(row["asset"])
        amount = int(row.get("estimated_yield_raw") or 0)
        totals.append(
            {
                "asset": asset,
                "estimated_yield_raw": amount,
                "estimated_yield": _human(amount, asset),
            }
        )
    return {
        "wallet": addr,
        "positions": positions,
        "totals": totals,
        "limit": page["limit"],
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "as_of": page["as_of"],
        "accrued_as_of": page["accrued_as_of"],
    }


@router.get(
    "/yield/user/{address}/history",
    tags=["Yield"],
    summary="Distribution history with tx hashes",
    response_model=YieldHistoryResponse,
)
async def get_yield_history(
    address: str,
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    cursor: str | None = None,
):
    """Return a complete, snapshot-bound keyset page of allocations."""
    addr = _validate_address(address)
    try:
        payload = fetch_yield_page(
            get_client(),
            address=addr,
            stream="yield_history",
            limit=limit,
            cursor=cursor,
        )
        page = build_yield_page(
            payload,
            address=addr,
            stream="yield_history",
            limit=limit,
        )
    except PositionCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if "signing key" in str(exc):
            raise HTTPException(
                status_code=503, detail="Yield pagination is unavailable"
            ) from exc
        logger.exception("Failed to fetch yield history for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch history")
    except Exception:
        logger.exception("Failed to fetch yield history for %s", addr)
        raise HTTPException(status_code=502, detail="Could not fetch history")

    history = []
    for row in page["rows"]:
        amount = int(row["amount"])
        asset = str(row["asset"])
        history.append(
            {
                "id": str(row["id"]),
                "distribution_id": str(row["distribution_id"]),
                "asset": asset,
                "amount_raw": amount,
                "amount": _human(amount, asset),
                "status": str(row["status"]),
                "airdrop_tx_hash": row.get("airdrop_tx_hash"),
                "created_at": _iso(row["created_at"]),
            }
        )
    return {
        "wallet": addr,
        "history": history,
        "limit": page["limit"],
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "as_of": page["as_of"],
    }


@router.get(
    "/yield/stats",
    tags=["Yield"],
    summary="Global yield statistics",
    response_model=YieldStatsResponse,
)
async def get_yield_stats():
    """Return cached fixed-cardinality global aggregates and fresh accrued values."""
    try:
        stats = _get_stats_snapshot(get_client())
    except Exception:
        logger.exception("Failed to fetch yield stats")
        raise HTTPException(status_code=502, detail="Could not fetch stats")
    try:
        accrued = _get_accrued_snapshot()
        _require_current_accrued(accrued)
    except Exception:
        logger.exception("Failed to fetch accrued yield stats")
        raise HTTPException(status_code=502, detail="Could not fetch stats")

    rows_by_asset = {str(row["asset"]): row for row in stats.rows or []}
    assets = []
    for asset in _ASSET_ADDRESSES:
        row = rows_by_asset.get(asset, {})
        total_yield = int(row.get("total_yield_raw") or 0)
        total_fees = int(row.get("total_fees_raw") or 0)
        accrued_raw = accrued.values.get(asset)
        assets.append(
            {
                "asset": asset,
                "total_yield_raw": total_yield,
                "total_yield": _human(total_yield, asset),
                "total_fees_raw": total_fees,
                "total_fees": _human(total_fees, asset),
                "total_distributed": _human(total_yield - total_fees, asset),
                "distributions": int(row.get("distributions") or 0),
                "current_accrued_raw": accrued_raw,
                "current_accrued": _human(accrued_raw, asset),
            }
        )
    return {
        "assets": assets,
        "as_of": stats.as_of,
        "accrued_as_of": accrued.as_of,
    }
