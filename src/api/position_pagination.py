"""Bounded position reads and opaque, filter-bound keyset cursors."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from src.config import settings


PositionStream = Literal["active", "settled", "changes"]

CURSOR_VERSION = 1
PAGE_DEFAULT = 50
PAGE_MAX = 100
LEGACY_ACTIVE_LIMIT = 50
LEGACY_SETTLED_LIMIT = 20


class PositionCursorError(ValueError):
    """Raised when a cursor is malformed, tampered, or bound to other filters."""


@dataclass(frozen=True)
class PositionSubject:
    account_id: str | None = None
    privy_user_id: str | None = None
    wallets: tuple[tuple[str, str], ...] = ()
    scope: str | None = None

    def as_filter(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "privy_user_id": self.privy_user_id,
            "wallets": [list(wallet) for wallet in self.wallets],
            "scope": self.scope,
        }

    def rpc_params(self) -> dict[str, Any]:
        return {
            "p_account_id": self.account_id,
            "p_privy_user_id": self.privy_user_id,
            "p_wallets": [
                {"chain": chain, "address": address} for chain, address in self.wallets
            ]
            or None,
        }


def normalized_wallet_subject(
    wallets: list[tuple[str, str]], *, scope: str | None = None
) -> PositionSubject:
    normalized = sorted(
        {
            (chain, address.lower() if chain == "base" else address)
            for chain, address in wallets
        }
    )
    return PositionSubject(wallets=tuple(normalized), scope=scope)


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        if _urlsafe_encode(decoded) != value:
            raise PositionCursorError("Invalid position cursor")
        return decoded
    except (binascii.Error, ValueError, TypeError) as exc:
        raise PositionCursorError("Invalid position cursor") from exc


def _signing_key() -> bytes:
    value = settings.position_cursor_secret
    encoded = value.encode("utf-8")
    reused_supabase_key = any(
        candidate and hmac.compare_digest(value, candidate)
        for candidate in (
            settings.supabase_anon_key,
            settings.supabase_service_role_key,
        )
    )
    if len(encoded) < 32 or reused_supabase_key:
        raise RuntimeError("Position cursor signing key is unavailable or invalid")
    return encoded


def _valid_wallet_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _filter_hash(
    subject: PositionSubject,
    stream: str,
    changed_after: str | None,
) -> str:
    value = {
        "subject": subject.as_filter(),
        "stream": stream,
        "changed_after": changed_after,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_watermark(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def encode_position_cursor(
    *,
    subject: PositionSubject,
    stream: str,
    changed_after: str | None,
    key_at: str,
    key_id: str,
    watermark: str,
    wallet_fingerprint: str,
    provenance: dict[str, Any] | None = None,
) -> str:
    try:
        normalized_key_at = normalize_watermark(key_at)
        normalized_watermark = normalize_watermark(watermark)
        UUID(key_id)
    except (TypeError, ValueError) as exc:
        raise PositionCursorError("Invalid position cursor key") from exc
    if normalized_key_at is None or normalized_watermark is None:
        raise PositionCursorError("Invalid position cursor key")
    if not _valid_wallet_fingerprint(wallet_fingerprint):
        raise PositionCursorError("Invalid wallet-set fingerprint")
    payload = {
        "v": CURSOR_VERSION,
        "f": _filter_hash(subject, stream, changed_after),
        "k": [normalized_key_at, key_id],
        "s": wallet_fingerprint,
        "w": normalized_watermark,
    }
    if provenance is not None:
        payload["p"] = provenance
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise PositionCursorError("Invalid position cursor provenance") from exc
    if len(encoded) > 2048:
        raise PositionCursorError("Position cursor provenance is too large")
    signature = hmac.new(_signing_key(), encoded, hashlib.sha256).digest()
    return f"v{CURSOR_VERSION}.{_urlsafe_encode(encoded)}.{_urlsafe_encode(signature)}"


def decode_position_cursor(
    token: str,
    *,
    subject: PositionSubject,
    stream: str,
    changed_after: str | None,
) -> dict[str, Any]:
    try:
        if len(token) > 4096:
            raise PositionCursorError("Invalid position cursor")
        prefix, encoded_payload, encoded_signature = token.split(".", 2)
        payload_bytes = _urlsafe_decode(encoded_payload)
        signature = _urlsafe_decode(encoded_signature)
        expected = hmac.new(_signing_key(), payload_bytes, hashlib.sha256).digest()
        if prefix != f"v{CURSOR_VERSION}" or not hmac.compare_digest(
            signature, expected
        ):
            raise PositionCursorError("Invalid position cursor")
        payload = json.loads(payload_bytes)
        key = payload["k"]
        if (
            payload.get("v") != CURSOR_VERSION
            or payload.get("f") != _filter_hash(subject, stream, changed_after)
            or not isinstance(key, list)
            or len(key) != 2
            or not all(isinstance(item, str) and item for item in key)
            or not _valid_wallet_fingerprint(payload.get("s"))
            or not isinstance(payload.get("w"), str)
        ):
            raise PositionCursorError("Position cursor does not match this request")
        normalize_watermark(key[0])
        normalize_watermark(payload["w"])
        UUID(key[1])
    except PositionCursorError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PositionCursorError("Invalid position cursor") from exc
    decoded = {
        "key_at": key[0],
        "key_id": key[1],
        "watermark": payload["w"],
        "wallet_fingerprint": payload["s"],
    }
    if "p" in payload:
        decoded["provenance"] = payload["p"]
    return decoded


def _rpc_payload(result: Any) -> dict[str, Any]:
    payload = result.data
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError("Position RPC returned an invalid payload")
    return payload


def fetch_position_rpc(
    client: Any,
    *,
    subject: PositionSubject,
    stream: PositionStream | Literal["snapshot"],
    limit: int = PAGE_DEFAULT,
    cursor: str | None = None,
    changed_after: datetime | str | None = None,
) -> dict[str, Any]:
    normalized_after = normalize_watermark(changed_after)
    if stream == "changes" and normalized_after is None:
        raise PositionCursorError("changed_after is required for the changes stream")
    if stream != "changes" and normalized_after is not None:
        raise PositionCursorError("changed_after is only valid for the changes stream")
    if stream == "snapshot" and cursor is not None:
        raise PositionCursorError("snapshot cursors use an explicit stream")

    cursor_values: dict[str, str | None] = {
        "key_at": None,
        "key_id": None,
        "watermark": None,
        "wallet_fingerprint": None,
    }
    if cursor is not None:
        if stream == "snapshot":
            raise PositionCursorError("Invalid snapshot cursor")
        cursor_values = decode_position_cursor(
            cursor,
            subject=subject,
            stream=stream,
            changed_after=normalized_after,
        )

    params = {
        **subject.rpc_params(),
        "p_stream": stream,
        "p_limit": limit,
        "p_active_limit": LEGACY_ACTIVE_LIMIT,
        "p_settled_limit": LEGACY_SETTLED_LIMIT,
        "p_cursor_at": cursor_values["key_at"],
        "p_cursor_id": cursor_values["key_id"],
        "p_changed_after": normalized_after,
        "p_watermark": cursor_values["watermark"],
        "p_wallet_fingerprint": cursor_values["wallet_fingerprint"],
    }
    result = client.rpc("b1nary_position_page", params).execute()
    payload = _rpc_payload(result)
    if payload.get("filter_mismatch") is True:
        raise PositionCursorError("Position cursor wallet filter changed")
    wallet_fingerprint = payload.get("wallet_fingerprint")
    if not _valid_wallet_fingerprint(wallet_fingerprint):
        raise RuntimeError("Position RPC omitted wallet-set fingerprint")
    expected_fingerprint = cursor_values["wallet_fingerprint"]
    if expected_fingerprint is not None and not hmac.compare_digest(
        wallet_fingerprint,
        expected_fingerprint,
    ):
        raise PositionCursorError("Position cursor wallet filter changed")
    payload.setdefault("account_found", True)
    payload.setdefault("watermark", cursor_values["watermark"])
    return payload


def _cursor_key(row: dict[str, Any], stream: PositionStream) -> tuple[str, str]:
    if stream == "active":
        key_at = row.get("indexed_at")
    elif stream == "settled":
        key_at = row.get("settled_at") or row.get("updated_at")
    else:
        key_at = row.get("updated_at")
    key_id = row.get("id")
    if not isinstance(key_at, str) or not isinstance(key_id, str):
        raise RuntimeError("Position RPC omitted cursor keys")
    return key_at, key_id


def build_position_page(
    payload: dict[str, Any],
    *,
    subject: PositionSubject,
    stream: PositionStream,
    limit: int,
    changed_after: datetime | str | None,
) -> dict[str, Any]:
    rows = payload.get("rows") or []
    if not isinstance(rows, list):
        raise RuntimeError("Position RPC returned invalid rows")
    watermark = normalize_watermark(payload.get("watermark"))
    if watermark is None:
        raise RuntimeError("Position RPC omitted watermark")
    wallet_fingerprint = payload.get("wallet_fingerprint")
    if not _valid_wallet_fingerprint(wallet_fingerprint):
        raise RuntimeError("Position RPC omitted wallet-set fingerprint")
    visible = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = None
    if has_more and visible:
        key_at, key_id = _cursor_key(visible[-1], stream)
        next_cursor = encode_position_cursor(
            subject=subject,
            stream=stream,
            changed_after=normalize_watermark(changed_after),
            key_at=key_at,
            key_id=key_id,
            watermark=watermark,
            wallet_fingerprint=wallet_fingerprint,
        )
    return {
        "positions": visible,
        "stream": stream,
        "limit": limit,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "watermark": watermark,
    }


def build_position_snapshot(
    payload: dict[str, Any],
    *,
    subject: PositionSubject,
) -> dict[str, Any]:
    active = payload.get("active") or []
    settled = payload.get("settled") or []
    if not isinstance(active, list) or not isinstance(settled, list):
        raise RuntimeError("Position RPC returned an invalid snapshot")
    watermark = normalize_watermark(payload.get("watermark"))
    if watermark is None:
        raise RuntimeError("Position RPC omitted watermark")
    wallet_fingerprint = payload.get("wallet_fingerprint")
    if not _valid_wallet_fingerprint(wallet_fingerprint):
        raise RuntimeError("Position RPC omitted wallet-set fingerprint")

    active_visible = active[:LEGACY_ACTIVE_LIMIT]
    settled_visible = settled[:LEGACY_SETTLED_LIMIT]

    def metadata(rows: list[dict], visible: list[dict], stream: PositionStream):
        has_more = len(rows) > len(visible)
        page_limit = LEGACY_ACTIVE_LIMIT if stream == "active" else LEGACY_SETTLED_LIMIT
        next_cursor = None
        if has_more and visible:
            key_at, key_id = _cursor_key(visible[-1], stream)
            next_cursor = encode_position_cursor(
                subject=subject,
                stream=stream,
                changed_after=None,
                key_at=key_at,
                key_id=key_id,
                watermark=watermark,
                wallet_fingerprint=wallet_fingerprint,
            )
        return {
            "limit": page_limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def legacy_sort_key(row: dict[str, Any]) -> tuple[datetime, str]:
        indexed_at = row.get("indexed_at")
        row_id = row.get("id")
        if not isinstance(indexed_at, str) or not isinstance(row_id, str):
            raise RuntimeError("Position RPC omitted legacy ordering keys")
        parsed = datetime.fromisoformat(indexed_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc), row_id

    legacy_positions = sorted(
        active_visible + settled_visible,
        key=legacy_sort_key,
        reverse=True,
    )

    return {
        "positions": legacy_positions,
        "watermark": watermark,
        "bounded": True,
        "active": metadata(active, active_visible, "active"),
        "settled": metadata(settled, settled_visible, "settled"),
    }
