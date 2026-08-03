"""Yield keyset pages using the B1N-430 signed cursor contract."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, Literal

from src.api.position_pagination import (
    PAGE_DEFAULT,
    PAGE_MAX,
    PositionCursorError,
    decode_position_cursor,
    encode_position_cursor,
    normalize_watermark,
    normalized_wallet_subject,
)


YieldStream = Literal["yield_positions", "yield_history"]
_POSITION_ASSETS = ("btc", "eth", "usdc")
_PROVENANCE_KEY = "_yield_position_provenance"


def _payload(result: Any) -> dict[str, Any]:
    payload = result.data
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError("Yield RPC returned an invalid payload")
    return payload


def _subject(address: str, stream: YieldStream):
    return normalized_wallet_subject(
        [("base", address)],
        scope=stream,
    )


def _position_provenance(
    as_of: datetime | str | None,
    accrued: dict[str, int | None] | None,
    protocol_fee_bps: int | None,
    period_start: datetime | str | None = None,
) -> dict[str, Any]:
    snapshot = normalize_watermark(as_of)
    if snapshot is None or accrued is None:
        raise ValueError("as_of and accrued values are required for yield positions")
    if set(accrued) != set(_POSITION_ASSETS):
        raise ValueError("accrued values must contain the supported yield assets")
    values = []
    for asset in _POSITION_ASSETS:
        value = accrued[asset]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError("accrued values must be non-negative integers or null")
        values.append(value)
    if (
        isinstance(protocol_fee_bps, bool)
        or not isinstance(protocol_fee_bps, int)
        or protocol_fee_bps < 0
        or protocol_fee_bps > 10_000
    ):
        raise ValueError("protocol fee must be between 0 and 10000 basis points")
    # Compact, fixed-cardinality provenance keeps continuation cursors bounded.
    provenance = {"a": snapshot, "r": values, "f": protocol_fee_bps}
    if period_start is not None:
        boundary = normalize_watermark(period_start)
        if boundary is None:
            raise ValueError("period start is required for yield position provenance")
        provenance["t"] = boundary
    return provenance


def _recover_position_provenance(
    provenance: Any,
    *,
    watermark: str | None,
) -> tuple[str, dict[str, int | None], int, str, dict[str, Any]]:
    try:
        if not isinstance(provenance, dict) or set(provenance) != {
            "a",
            "r",
            "f",
            "t",
        }:
            raise ValueError
        raw_values = provenance["r"]
        if not isinstance(raw_values, list) or len(raw_values) != len(_POSITION_ASSETS):
            raise ValueError
        accrued = dict(zip(_POSITION_ASSETS, raw_values, strict=True))
        normalized = _position_provenance(
            provenance["a"],
            accrued,
            provenance["f"],
            provenance["t"],
        )
        snapshot = normalized["a"]
        if normalize_watermark(watermark) != snapshot:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise PositionCursorError("Invalid yield position cursor provenance") from exc
    return snapshot, accrued, normalized["f"], normalized["t"], normalized


def fetch_yield_page(
    client: Any,
    *,
    address: str,
    stream: YieldStream,
    limit: int = PAGE_DEFAULT,
    cursor: str | None = None,
    as_of: datetime | str | None = None,
    accrued: dict[str, int | None] | None = None,
    protocol_fee_bps: int | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > PAGE_MAX:
        raise PositionCursorError("Yield page limit must be between 1 and 100")
    subject = _subject(address, stream)
    cursor_values: dict[str, Any] = {
        "key_at": None,
        "key_id": None,
        "watermark": None,
        "wallet_fingerprint": None,
    }
    if cursor is not None:
        cursor_values = decode_position_cursor(
            cursor,
            subject=subject,
            stream=stream,
            changed_after=None,
        )

    params: dict[str, Any] = {
        "p_user_address": address,
        "p_limit": limit,
        "p_cursor_at": cursor_values["key_at"],
        "p_cursor_id": cursor_values["key_id"],
        "p_watermark": cursor_values["watermark"],
        "p_wallet_fingerprint": cursor_values["wallet_fingerprint"],
    }
    rpc_name = "b1nary_yield_history_page"
    provenance = None
    if stream == "yield_positions":
        rpc_name = "b1nary_yield_position_page"
        if cursor is None:
            provenance = _position_provenance(as_of, accrued, protocol_fee_bps)
            snapshot, snapshot_accrued, snapshot_fee, snapshot_period_start = (
                provenance["a"],
                dict(zip(_POSITION_ASSETS, provenance["r"], strict=True)),
                provenance["f"],
                None,
            )
            # Position estimates and membership share the observed financial
            # snapshot; the RPC must echo it as the continuation watermark.
            params["p_watermark"] = snapshot
        else:
            (
                snapshot,
                snapshot_accrued,
                snapshot_fee,
                snapshot_period_start,
                provenance,
            ) = _recover_position_provenance(
                cursor_values.get("provenance"),
                watermark=cursor_values["watermark"],
            )
        params["p_as_of"] = snapshot
        params["p_period_start"] = snapshot_period_start
        params["p_accrued"] = snapshot_accrued
        params["p_protocol_fee_bps"] = snapshot_fee

    payload = _payload(client.rpc(rpc_name, params).execute())
    if payload.get("filter_mismatch") is True:
        raise PositionCursorError("Yield cursor wallet filter changed")
    fingerprint = payload.get("wallet_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise RuntimeError("Yield RPC omitted wallet fingerprint")
    expected = cursor_values["wallet_fingerprint"]
    if expected is not None and not hmac.compare_digest(expected, fingerprint):
        raise PositionCursorError("Yield cursor wallet filter changed")

    if stream == "yield_positions":
        payload_watermark = normalize_watermark(payload.get("watermark"))
        payload_as_of = normalize_watermark(payload.get("as_of"))
        payload_period_start = normalize_watermark(payload.get("period_start"))
        if payload_watermark != snapshot or payload_as_of != snapshot:
            raise RuntimeError("Yield RPC returned a mismatched financial snapshot")
        if cursor is None:
            if payload_period_start is None:
                raise RuntimeError("Yield RPC omitted the financial period boundary")
            provenance = _position_provenance(
                snapshot,
                snapshot_accrued,
                snapshot_fee,
                payload_period_start,
            )
        elif payload_period_start != snapshot_period_start:
            raise RuntimeError(
                "Yield RPC returned a mismatched financial period boundary"
            )
        payload[_PROVENANCE_KEY] = provenance
    return payload


def build_yield_page(
    payload: dict[str, Any],
    *,
    address: str,
    stream: YieldStream,
    limit: int,
) -> dict[str, Any]:
    rows = payload.get("rows") or []
    if not isinstance(rows, list):
        raise RuntimeError("Yield RPC returned invalid rows")
    watermark = normalize_watermark(payload.get("watermark"))
    if watermark is None:
        raise RuntimeError("Yield RPC omitted watermark")
    fingerprint = payload.get("wallet_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise RuntimeError("Yield RPC omitted wallet fingerprint")

    provenance = payload.get(_PROVENANCE_KEY)
    accrued = None
    accrued_as_of = None
    protocol_fee_bps = None
    period_start = None
    if stream == "yield_positions":
        accrued_as_of, accrued, protocol_fee_bps, period_start, provenance = (
            _recover_position_provenance(provenance, watermark=watermark)
        )

    visible = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = None
    if has_more and visible:
        key_name = "deposited_at" if stream == "yield_positions" else "created_at"
        key_at = visible[-1].get(key_name)
        key_id = visible[-1].get("id")
        if not isinstance(key_at, str) or not isinstance(key_id, str):
            raise RuntimeError("Yield RPC omitted cursor keys")
        next_cursor = encode_position_cursor(
            subject=_subject(address, stream),
            stream=stream,
            changed_after=None,
            key_at=key_at,
            key_id=key_id,
            watermark=watermark,
            wallet_fingerprint=fingerprint,
            provenance=provenance,
        )

    return {
        "rows": visible,
        "limit": limit,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "as_of": watermark,
        "accrued": accrued,
        "accrued_as_of": accrued_as_of,
        "protocol_fee_bps": protocol_fee_bps,
        "period_start": period_start,
    }
