"""Bounded legacy Earnings Challenge leaderboard reads.

The legacy routes are mounted only when ``LEGACY_AGORA_V1_ENABLED`` is opted in.
All ranking work stays in PostgreSQL; this module validates the fixed response,
adds a bounded process-local cache, and never transfers raw ``order_events``.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter()

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Competition defaults: 2026-03-30 00:00 UTC -> 2026-04-12 23:59:59 UTC.
_DEFAULT_START = 1774828800
_DEFAULT_END = 1776038399
_MAX_RANGE_SECS = 90 * 24 * 3600
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100

_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ENTRIES = 256
_cache: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()


def _monotonic() -> float:
    """Keep the cache clock patchable without replacing the process clock."""
    return time.monotonic()


def _validate_range(start: int, end: int) -> None:
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    if end - start > _MAX_RANGE_SECS:
        raise HTTPException(status_code=400, detail="Range must not exceed 90 days")


def _rpc_payload(result: Any, *, name: str) -> dict[str, Any]:
    payload = result.data
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError(f"{name} RPC returned an invalid payload")
    return payload


def _validate_as_of(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError("Leaderboard RPC omitted as_of")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Leaderboard RPC returned invalid as_of") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("Leaderboard RPC returned naive as_of")
    return value


def _wire_round(value: Any, *, field: str) -> float:
    """Apply the predecessor's Python float rounding to an internal SQL value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"Leaderboard RPC returned invalid {field}")
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(f"Leaderboard RPC returned invalid {field}")
    return round(number, 2)


def _normalize_leaderboard_payload(
    payload: dict[str, Any], limit: int
) -> dict[str, Any]:
    normalized = deepcopy(payload)
    track1 = normalized.get("track1")
    track2 = normalized.get("track2")
    meta = normalized.get("meta")
    if not isinstance(track1, list) or not isinstance(track2, list):
        raise RuntimeError("Leaderboard RPC returned invalid tracks")
    if len(track1) > limit or len(track2) > limit:
        raise RuntimeError("Leaderboard RPC exceeded requested cardinality")
    if not isinstance(meta, dict):
        raise RuntimeError("Leaderboard RPC returned invalid metadata")
    _validate_as_of(meta.get("as_of"))
    if meta.get("limit") != limit:
        raise RuntimeError("Leaderboard RPC returned inconsistent limit")

    for entry in track1:
        if not isinstance(entry, dict):
            raise RuntimeError("Leaderboard RPC returned invalid Track 1 entry")
        raw = entry.pop("_internal_raw_collateral_usd", None)
        entry["total_collateral_usd"] = _wire_round(raw, field="raw wallet collateral")
    raw_total = meta.pop("_internal_raw_total_volume_usd", None)
    meta["total_volume_usd"] = _wire_round(raw_total, field="raw aggregate collateral")
    return normalized


def _normalize_me_payload(payload: dict[str, Any], address: str) -> dict[str, Any]:
    normalized = deepcopy(payload)
    if normalized.get("wallet") != address:
        raise RuntimeError("Leaderboard/me RPC returned another wallet")
    _validate_as_of(normalized.get("as_of"))
    raw = normalized.pop("_internal_raw_collateral_usd", None)
    normalized["total_collateral_usd"] = _wire_round(raw, field="raw wallet collateral")
    return normalized


def _cache_get(key: tuple[Any, ...]) -> tuple[dict[str, Any], float] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    cached_at, payload = entry
    age = _monotonic() - cached_at
    if age >= _CACHE_TTL_SECONDS:
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return deepcopy(payload), age


def _cache_put(key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    _cache[key] = (_monotonic(), deepcopy(payload))
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def _decorate_freshness(
    payload: dict[str, Any], *, cache_hit: bool, cache_age: float, nested: bool
) -> dict[str, Any]:
    decorated = deepcopy(payload)
    target = decorated["meta"] if nested else decorated
    target["cache_ttl_seconds"] = _CACHE_TTL_SECONDS
    target["cache_age_seconds"] = round(max(cache_age, 0.0), 3)
    target["cache_hit"] = cache_hit
    return decorated


def _set_cache_headers(
    response: Response,
    payload: dict[str, Any],
    *,
    nested: bool,
    private: bool,
) -> None:
    source = payload["meta"] if nested else payload
    visibility = "private" if private else "public"
    response.headers["Cache-Control"] = f"{visibility}, max-age={_CACHE_TTL_SECONDS}"
    response.headers["Age"] = str(int(source["cache_age_seconds"]))
    response.headers["X-Data-As-Of"] = source["as_of"]


@router.get(
    "/leaderboard", tags=["Leaderboard"], summary="Earnings Challenge leaderboard"
)
async def get_leaderboard(
    response: Response,
    start: int = Query(default=_DEFAULT_START),
    end: int = Query(default=_DEFAULT_END),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
):
    """Return the two legacy prize tracks from one bounded SQL snapshot.

    Qualification remains collateral-only at $500. ``limit`` applies to each
    track; metadata always describes every participant in the requested window.
    """
    _validate_range(start, end)
    key = ("leaderboard", start, end, limit)
    cached = _cache_get(key)
    if cached is not None:
        payload, age = cached
        decorated = _decorate_freshness(
            payload, cache_hit=True, cache_age=age, nested=True
        )
        _set_cache_headers(response, decorated, nested=True, private=False)
        return decorated

    try:
        result = (
            get_client()
            .rpc(
                "b1nary_legacy_leaderboard",
                {"p_start": start, "p_end": end, "p_limit": limit},
            )
            .execute()
        )
        payload = _normalize_leaderboard_payload(
            _rpc_payload(result, name="Leaderboard"), limit
        )
    except Exception:
        logger.exception("Failed to fetch bounded leaderboard snapshot")
        raise HTTPException(status_code=502, detail="Could not fetch leaderboard data")

    _cache_put(key, payload)
    decorated = _decorate_freshness(
        payload, cache_hit=False, cache_age=0.0, nested=True
    )
    _set_cache_headers(response, decorated, nested=True, private=False)
    return decorated


@router.get(
    "/leaderboard/me",
    tags=["Leaderboard"],
    summary="Personal Earnings Challenge stats (no eligibility filter)",
)
async def get_leaderboard_me(
    response: Response,
    address: str = Query(...),
    start: int = Query(default=_DEFAULT_START),
    end: int = Query(default=_DEFAULT_END),
):
    """Return one fixed-cardinality wallet aggregate.

    ``qualifies`` reflects only the preserved $500 collateral threshold.
    """
    if not _ETH_ADDRESS_RE.fullmatch(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    _validate_range(start, end)

    normalized_address = address.lower()
    key = ("leaderboard/me", normalized_address, start, end)
    cached = _cache_get(key)
    if cached is not None:
        payload, age = cached
        decorated = _decorate_freshness(
            payload, cache_hit=True, cache_age=age, nested=False
        )
        _set_cache_headers(response, decorated, nested=False, private=True)
        return decorated

    try:
        result = (
            get_client()
            .rpc(
                "b1nary_legacy_leaderboard_me",
                {
                    "p_address": normalized_address,
                    "p_start": start,
                    "p_end": end,
                },
            )
            .execute()
        )
        payload = _normalize_me_payload(
            _rpc_payload(result, name="Leaderboard/me"), normalized_address
        )
    except Exception:
        logger.exception(
            "Failed to fetch bounded leaderboard stats for %s", normalized_address
        )
        raise HTTPException(status_code=502, detail="Could not fetch leaderboard data")

    _cache_put(key, payload)
    decorated = _decorate_freshness(
        payload, cache_hit=False, cache_age=0.0, nested=False
    )
    _set_cache_headers(response, decorated, nested=False, private=True)
    return decorated
