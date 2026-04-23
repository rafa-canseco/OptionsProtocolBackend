"""Index Solana BatchSettler executeOrder transactions into order_events."""

import asyncio
import base64
import hashlib
import json
import logging
import struct
from datetime import datetime, timezone
from functools import lru_cache

from solana.rpc.commitment import Finalized
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.signature import Signature  # type: ignore[import-untyped]

from src.api.mm_ws import notify_mm_fill
from src.chains.solana.client import get_solana_client
from src.config import has_solana_config, settings
from src.db.database import get_client
from src.pricing.assets import get_asset_config, get_solana_assets

logger = logging.getLogger(__name__)

CHAIN = "solana"
PAGE_LIMIT = 100
MAX_SUPPORTED_TX_VERSION = 0

_EXECUTE_ORDER_DISC = hashlib.sha256(b"event:OrderExecuted").digest()[:8]
_DEPOSIT_COLLATERAL_DISC = bytes.fromhex("f43e4d0b87703d60")
_ORDER_EXECUTED_LOG = "Program log: Instruction: ExecuteOrder"


def _utc_day_start_ts() -> int:
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _get_state() -> dict | None:
    client = get_client()
    result = (
        client.table("solana_indexer_state")
        .select("last_signature,last_slot")
        .eq("chain", CHAIN)
        .eq("program_id", settings.solana_batch_settler_program_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def _set_state(*, last_signature: str, last_slot: int) -> None:
    client = get_client()
    (
        client.table("solana_indexer_state")
        .upsert(
            {
                "chain": CHAIN,
                "program_id": settings.solana_batch_settler_program_id,
                "last_signature": last_signature,
                "last_slot": last_slot,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="chain,program_id",
        )
        .execute()
    )


def _fetch_signatures_page(
    *,
    before: str | None = None,
    until: str | None = None,
) -> list[dict]:
    rpc = get_solana_client()
    resp = rpc.get_signatures_for_address(
        Pubkey.from_string(settings.solana_batch_settler_program_id),
        before=Signature.from_string(before) if before else None,
        until=Signature.from_string(until) if until else None,
        limit=PAGE_LIMIT,
        commitment=Finalized,
    )
    payload = json.loads(resp.to_json())
    return payload.get("result") or []


def _collect_candidate_signatures() -> list[dict]:
    state = _get_state()
    until_sig = state.get("last_signature") if state else None
    min_block_time = None if until_sig else _utc_day_start_ts()

    collected: list[dict] = []
    before: str | None = None

    while True:
        page = _fetch_signatures_page(before=before, until=until_sig)
        if not page:
            break

        stop = False
        for item in page:
            if item.get("err") is not None:
                continue
            if item.get("confirmationStatus") not in (None, "confirmed", "finalized"):
                continue
            block_time = item.get("blockTime")
            if min_block_time is not None and block_time is not None and block_time < min_block_time:
                stop = True
                break
            collected.append(item)

        if stop or len(page) < PAGE_LIMIT:
            break
        before = page[-1]["signature"]

    return collected


def _get_transaction(signature: str) -> dict | None:
    rpc = get_solana_client()
    resp = rpc.get_transaction(
        Signature.from_string(signature),
        encoding="json",
        commitment=Finalized,
        max_supported_transaction_version=MAX_SUPPORTED_TX_VERSION,
    )
    payload = json.loads(resp.to_json())
    return payload.get("result")


def _decode_execute_event(data_b64: str) -> dict | None:
    raw = base64.b64decode(data_b64)
    if len(raw) != 136 or raw[:8] != _EXECUTE_ORDER_DISC:
        return None

    return {
        "user_address": str(Pubkey.from_bytes(raw[8:40])),
        "mm_address": str(Pubkey.from_bytes(raw[40:72])),
        "otoken_address": str(Pubkey.from_bytes(raw[72:104])),
        "amount": str(struct.unpack_from("<Q", raw, 104)[0]),
        "premium": str(struct.unpack_from("<Q", raw, 112)[0]),
        "gross_premium": str(struct.unpack_from("<Q", raw, 112)[0]),
        "net_premium": str(struct.unpack_from("<Q", raw, 120)[0]),
        "protocol_fee": str(struct.unpack_from("<Q", raw, 128)[0]),
    }


def _decode_deposit_collateral_event(data_b64: str) -> tuple[int, str] | None:
    raw = base64.b64decode(data_b64)
    if len(raw) != 88 or raw[:8] != _DEPOSIT_COLLATERAL_DISC:
        return None
    vault_id = struct.unpack_from("<Q", raw, 40)[0]
    collateral = struct.unpack_from("<Q", raw, 80)[0]
    return vault_id, str(collateral)


def _parse_log_payloads(logs: list[str]) -> dict | None:
    execute_event: dict | None = None
    deposit_event: tuple[int, str] | None = None

    for line in logs:
        if not line.startswith("Program data: "):
            continue

        payload = line.split("Program data: ", 1)[1].strip()
        decoded_execute = _decode_execute_event(payload)
        if decoded_execute:
            execute_event = decoded_execute
            continue

        decoded_deposit = _decode_deposit_collateral_event(payload)
        if decoded_deposit:
            deposit_event = decoded_deposit

    if not execute_event or not deposit_event:
        return None

    vault_id, collateral = deposit_event
    execute_event["vault_id"] = vault_id
    execute_event["collateral"] = collateral
    return execute_event


def _load_otoken_metadata_from_db(otoken_address: str) -> dict | None:
    client = get_client()
    result = (
        client.table("available_otokens")
        .select("strike_price,expiry,is_put,underlying")
        .eq("otoken_address", otoken_address)
        .eq("chain", CHAIN)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    return {
        "strike_price": row.get("strike_price"),
        "expiry": row.get("expiry"),
        "is_put": row.get("is_put"),
        "asset": _underlying_to_asset(row.get("underlying")),
    }


def _underlying_to_asset(underlying: str | None) -> str:
    if not underlying:
        return "unknown"

    lookup = {
        get_asset_config(asset).underlying_address: asset.value
        for asset in get_solana_assets()
    }
    return lookup.get(underlying, "unknown")


def _derive_otoken_info_pda(otoken_address: str) -> Pubkey:
    controller_program = Pubkey.from_string(settings.solana_controller_program_id)
    otoken_mint = Pubkey.from_string(otoken_address)
    return Pubkey.find_program_address(
        [b"otoken_info", bytes(otoken_mint)],
        controller_program,
    )[0]


def _load_otoken_metadata_from_chain(otoken_address: str) -> dict | None:
    rpc = get_solana_client()
    resp = rpc.get_account_info(_derive_otoken_info_pda(otoken_address))
    if resp.value is None:
        return None

    data = resp.value.data
    min_length = 8 + 32 + 32 + 32 + 32 + 8 + 8 + 1
    if len(data) < min_length:
        logger.warning(
            "otoken_info too short for %s: got %d bytes",
            otoken_address,
            len(data),
        )
        return None

    underlying = str(Pubkey.from_bytes(data[40:72]))
    return {
        "strike_price": struct.unpack_from("<Q", data, 136)[0],
        "expiry": struct.unpack_from("<q", data, 144)[0],
        "is_put": bool(data[152]),
        "asset": _underlying_to_asset(underlying),
    }


@lru_cache(maxsize=512)
def _load_otoken_metadata(otoken_address: str) -> dict:
    db_meta = _load_otoken_metadata_from_db(otoken_address)
    if db_meta:
        return db_meta

    chain_meta = _load_otoken_metadata_from_chain(otoken_address)
    if chain_meta:
        return chain_meta

    logger.warning("Could not load oToken metadata for %s", otoken_address)
    return {
        "strike_price": None,
        "expiry": None,
        "is_put": None,
        "asset": "unknown",
    }


def _build_order_row(signature: str, slot: int, tx: dict) -> dict | None:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None

    logs = meta.get("logMessages") or []
    if _ORDER_EXECUTED_LOG not in logs:
        return None

    event = _parse_log_payloads(logs)
    if not event:
        raise ValueError(f"Failed to decode executeOrder logs for tx={signature}")

    row = {
        "tx_hash": signature,
        "block_number": slot,
        "log_index": 0,
        "chain": CHAIN,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        **event,
        **_load_otoken_metadata(event["otoken_address"]),
    }
    return row


def _store_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    client = get_client()
    result = (
        client.table("order_events")
        .upsert(rows, on_conflict="tx_hash")
        .execute()
    )
    return len(result.data or [])


async def _notify_mm(rows: list[dict]) -> None:
    for row in rows:
        try:
            await notify_mm_fill(row["mm_address"], row)
        except Exception:
            logger.exception(
                "Failed MM fill notification for tx=%s mm=%s",
                row.get("tx_hash"),
                row.get("mm_address"),
            )


async def index_once() -> int:
    signatures = _collect_candidate_signatures()
    if not signatures:
        return 0

    rows: list[dict] = []
    newest_signature = signatures[0]["signature"]
    newest_slot = int(signatures[0]["slot"])

    for item in reversed(signatures):
        signature = item["signature"]
        slot = int(item["slot"])
        tx = _get_transaction(signature)
        if not tx:
            raise RuntimeError(
                f"Transaction missing for signature={signature} slot={slot}"
            )

        try:
            row = _build_order_row(signature, slot, tx)
        except Exception:
            logger.exception("Failed to decode Solana tx=%s slot=%d", signature, slot)
            raise

        if not row:
            continue

        rows.append(row)
        logger.info(
            "Decoded Solana fill tx=%s slot=%d user=%s vault_id=%s",
            signature,
            slot,
            row["user_address"],
            row["vault_id"],
        )

    stored = _store_rows(rows)
    if rows:
        await _notify_mm(rows)

    _set_state(last_signature=newest_signature, last_slot=newest_slot)
    logger.info(
        "Solana indexer advanced cursor signature=%s slot=%d stored=%d scanned=%d",
        newest_signature,
        newest_slot,
        stored,
        len(signatures),
    )
    return stored


async def run() -> None:
    if not has_solana_config():
        logger.error("Solana not configured, solana_event_indexer cannot start")
        return

    logger.info(
        "Solana event indexer starting (cluster=%s, interval=%ds, program=%s)",
        settings.solana_cluster,
        settings.event_poll_interval_seconds,
        settings.solana_batch_settler_program_id,
    )

    while True:
        try:
            await index_once()
        except Exception:
            logger.exception("Solana event indexing failed")
        await asyncio.sleep(settings.event_poll_interval_seconds)
