"""
Event Indexer Bot

Primary mode (WSS URL configured): subscribes to BatchSettler logs via
eth_subscribe for sub-second event detection. Falls back to getLogs
catchup on reconnect so no events are missed.

Fallback mode (no WSS URL): polls via getLogs every 30s (legacy behavior).

Stores OrderExecuted events and updates existing rows with PhysicalDelivery
delivery data in the order_events Supabase table.
Tracks last_indexed_block for resumability.
"""

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timezone

from web3 import AsyncWeb3, Web3, WebSocketProvider

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import get_batch_settler, get_otoken, get_w3
from src.api.mm_ws import notify_mm_fill
from src.pricing.chainlink import get_asset_price
from src.pricing.assets import Asset
from src.pricing.utils import collateral_to_usd, strike_to_8_decimals

logger = logging.getLogger(__name__)

BLOCK_RANGE = 2000  # max blocks per getLogs query
CONFIRMATION_BLOCKS = 2  # wait N blocks before indexing (Base has fast finality)
RESCAN_BLOCKS = 50  # re-scan last N blocks each cycle to catch missed events
MAX_RECONNECT_DELAY = 60  # cap exponential backoff at 60s
_OTOKEN_CACHE_MAX = 4096  # bounded LRU to avoid unbounded growth on long-running bot

# Event topic hashes (keccak256 of canonical signature) — computed once
_ORDER_EXECUTED_TOPIC = Web3.keccak(
    text=(
        "OrderExecuted(address,address,address,"
        "uint256,uint256,uint256,uint256,uint256,uint256)"
    )
)
_PHYSICAL_DELIVERY_TOPIC = Web3.keccak(
    text="PhysicalDelivery(address,address,uint256,uint256)"
)
_otoken_metadata_cache: OrderedDict[str, dict] = OrderedDict()
# Lowest block with a known enrichment failure. The cursor must not
# advance past `_pending_failure_block - 1` in either the subscription
# or forward-pass path — otherwise a settlement-critical event could
# be lost permanently if the failure outlives the rescan window.
# Cleared when a forward pass indexes past the failure successfully.
_pending_failure_block: int | None = None


def _get_last_indexed_block() -> int:
    client = get_client()
    result = (
        client.table("indexer_state").select("last_indexed_block").eq("id", 1).execute()
    )
    if result.data:
        return result.data[0]["last_indexed_block"]
    return 0


def _set_last_indexed_block(block: int) -> None:
    client = get_client()
    client.table("indexer_state").upsert(
        {
            "id": 1,
            "last_indexed_block": block,
        }
    ).execute()


def _underlying_to_asset(underlying_addr: str) -> str:
    """Map an underlying token address to an asset symbol."""
    if not isinstance(underlying_addr, str):
        underlying_addr = settings.weth_address
    addr = underlying_addr.lower()
    weth = settings.weth_address.lower()
    wbtc = settings.wbtc_address.lower()
    if addr == weth:
        return "eth"
    if addr == wbtc:
        return "btc"
    return "unknown"


def _load_otoken_metadata_from_db(otoken_address: str) -> dict | None:
    """Read oToken metadata from available_otokens when present.

    available_otokens stores strike in human USD units, while order_events
    expects the contract's 8-decimal integer representation.
    """
    client = get_client()
    result = (
        client.table("available_otokens")
        .select("strike_price,expiry,is_put,underlying")
        .eq("otoken_address", otoken_address.lower())
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    if (
        row.get("strike_price") is None
        or row.get("expiry") is None
        or row.get("is_put") is None
    ):
        return None

    underlying = str(row.get("underlying") or settings.weth_address).lower()
    return {
        "strike_price": strike_to_8_decimals(float(row["strike_price"])),
        "expiry": int(row["expiry"]),
        "is_put": bool(row["is_put"]),
        "underlying": underlying,
        "asset": _underlying_to_asset(underlying),
    }


def _load_otoken_metadata_from_chain(otoken_address: str) -> dict:
    ot = get_otoken(otoken_address)
    strike = ot.functions.strikePrice().call()
    expiry = ot.functions.expiry().call()
    is_put = ot.functions.isPut().call()
    underlying_raw = ot.functions.underlying().call()
    underlying = (
        underlying_raw.lower()
        if isinstance(underlying_raw, str)
        else settings.weth_address.lower()
    )
    return {
        "strike_price": strike,
        "expiry": expiry,
        "is_put": is_put,
        "underlying": underlying,
        "asset": _underlying_to_asset(underlying),
    }


def _load_otoken_metadata(otoken_address: str) -> dict | None:
    """Resolve oToken metadata via DB-first, chain fallback, bounded LRU cache.

    Cache is capped at _OTOKEN_CACHE_MAX entries (oldest evicted first)
    so a long-running indexer cannot grow memory without bound.
    """
    addr = otoken_address.lower()
    if addr in _otoken_metadata_cache:
        _otoken_metadata_cache.move_to_end(addr)
        return _otoken_metadata_cache[addr]

    metadata = _load_otoken_metadata_from_db(addr)
    source = "db"
    if metadata is None:
        metadata = _load_otoken_metadata_from_chain(addr)
        source = "chain"
        logger.info("Loaded oToken metadata from chain fallback: %s", addr)
    else:
        logger.debug("Loaded oToken metadata from %s: %s", source, addr)

    _otoken_metadata_cache[addr] = metadata
    if len(_otoken_metadata_cache) > _OTOKEN_CACHE_MAX:
        _otoken_metadata_cache.popitem(last=False)
    return metadata


def _enrich_with_otoken_metadata(event_data: dict) -> dict | None:
    """Read oToken on-chain metadata for denormalization into DB.

    Returns the enriched event_data, or None if any required field
    (strike_price/expiry/is_put) could not be resolved. Returning None
    signals the caller to skip storage — we MUST NOT upsert a row
    missing these fields because identify_itm_positions relies on them
    for settlement and a missing strike could cause a payout to be
    incorrectly classified as OTM.
    """
    try:
        metadata = _load_otoken_metadata(event_data["otoken_address"])
    except Exception:
        logger.exception(
            "Could not read oToken metadata for %s. "
            "Skipping storage until next rescan.",
            event_data["otoken_address"],
        )
        return None
    if metadata is None:
        logger.error(
            "oToken metadata returned None for %s. Skipping storage until next rescan.",
            event_data["otoken_address"],
        )
        return None
    # Assign only after all reads succeed — no partial enrichment
    event_data["strike_price"] = metadata["strike_price"]
    event_data["expiry"] = metadata["expiry"]
    event_data["is_put"] = metadata["is_put"]
    event_data["asset"] = metadata["asset"]
    return event_data


def _enrich_with_collateral_usd(event_data: dict) -> dict:
    """Compute collateral_usd from Chainlink spot prices and attach to event_data.

    PUT options use USDC collateral — no Chainlink call needed.
    CALL options fetch only the relevant asset's spot price.
    Sets collateral_usd to None on RPC failure; the backfill script can fill the gap.
    """
    is_put = event_data.get("is_put")
    asset = event_data.get("asset") or "eth"

    if is_put is True or is_put is None:
        # PUT: USDC collateral, conversion is purely arithmetic
        event_data["collateral_usd"] = collateral_to_usd(event_data, 0.0, 0.0)
        return event_data

    try:
        if asset == "btc":
            btc_spot, _ = get_asset_price(Asset.BTC)
            event_data["collateral_usd"] = collateral_to_usd(event_data, 0.0, btc_spot)
        else:
            eth_spot, _ = get_asset_price(Asset.ETH)
            event_data["collateral_usd"] = collateral_to_usd(event_data, eth_spot, 0.0)
    except Exception:
        logger.warning(
            "Could not fetch Chainlink spot for %s CALL tx=%s. Will be backfilled later.",
            asset,
            event_data.get("tx_hash"),
        )
        event_data["collateral_usd"] = None
    return event_data


def _store_events(events: list[dict]) -> int:
    """Insert events into Supabase. Returns count inserted.

    Uses upsert on tx_hash so re-scanned events are safely deduplicated.
    Raises if Supabase accepts the request but returns empty data for a
    non-empty input — prevents the block pointer from advancing past lost events.
    Also raises if any event is missing a settlement-critical field, which
    would otherwise poison downstream ITM/OTM classification.
    """
    if not events:
        return 0
    for ev in events:
        for critical in ("strike_price", "expiry", "is_put"):
            if ev.get(critical) is None:
                raise ValueError(
                    f"Refusing to store order_event with missing {critical}: "
                    f"tx={ev.get('tx_hash')} otoken={ev.get('otoken_address')}"
                )
    client = get_client()
    result = (
        client.table("order_events")
        .upsert(
            events,
            on_conflict="tx_hash",
        )
        .execute()
    )
    if not result.data:
        logger.error(
            "_store_events: Supabase returned empty data for %d events",
            len(events),
        )
        raise RuntimeError(f"Supabase upsert returned no data for {len(events)} events")
    # Best-effort lifecycle telemetry. Order history remains canonical if this
    # enrichment write is temporarily unavailable.
    first_fill_at = datetime.now(timezone.utc).isoformat()
    addresses = sorted({event["otoken_address"].lower() for event in events})
    try:
        (
            client.table("available_otokens")
            .update({"first_filled_at": first_fill_at})
            .eq("chain", "base")
            .in_("otoken_address", addresses)
            .is_("first_filled_at", "null")
            .execute()
        )
    except Exception:
        logger.warning(
            "Could not record first-fill telemetry for %d oTokens",
            len(addresses),
            exc_info=True,
        )
    return len(result.data)


def _update_delivery_events(delivery_events: list[dict]) -> int:
    """Update existing order_events rows with physical delivery data.

    PhysicalDelivery events are emitted once per vault but the event itself
    only carries (oToken, user, contraAmount, collateralUsed) — no vault_id.
    A user with N vaults on the same oToken produces N events. Matching by
    (user, otoken) and updating all matching rows would overwrite each row
    N times with the last-seen event's tx and amount. Instead, claim one
    row per event:

      1. Idempotency: if a row already records this exact tx_hash, no-op.
      2. Otherwise pick the oldest unmarked row for (user, otoken) and
         write the event's data into that single row.

    Ordering by vault_id ASC keeps assignment deterministic across re-indexes.
    """
    if not delivery_events:
        return 0
    client = get_client()
    updated = 0
    for ev in delivery_events:
        already = (
            client.table("order_events")
            .select("id")
            .eq("user_address", ev["user_address"])
            .eq("otoken_address", ev["otoken_address"])
            .eq("delivery_tx_hash", ev["delivery_tx_hash"])
            .limit(1)
            .execute()
        )
        if already.data:
            continue

        candidates = (
            client.table("order_events")
            .select("id")
            .eq("user_address", ev["user_address"])
            .eq("otoken_address", ev["otoken_address"])
            .is_("delivery_tx_hash", "null")
            .order("vault_id")
            .limit(1)
            .execute()
        )
        if not candidates.data:
            logger.warning(
                "Physical delivery event matched no unmarked DB row "
                "(possibly already processed by the bot itself): "
                "user=%s otoken=%s tx=%s",
                ev["user_address"],
                ev["otoken_address"],
                ev["delivery_tx_hash"],
            )
            continue

        row_id = candidates.data[0]["id"]
        result = (
            client.table("order_events")
            .update(
                {
                    "settlement_type": "physical",
                    "delivered_asset": ev["delivered_asset"],
                    "delivered_amount": ev["delivered_amount"],
                    "delivery_tx_hash": ev["delivery_tx_hash"],
                    "is_itm": True,
                }
            )
            .eq("id", row_id)
            .execute()
        )
        if result.data:
            updated += len(result.data)
    return updated


def _notify_mm(event_data: dict) -> None:
    """Push fill notification to connected MM WebSocket clients."""
    mm_addr = event_data.get("mm_address")
    if not mm_addr:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop (sync context / tests)
    try:
        loop.create_task(notify_mm_fill(mm_addr, event_data))
    except RuntimeError:
        logger.warning(
            "Could not schedule MM fill notification for %s (loop closing?), tx=%s",
            mm_addr,
            event_data.get("tx_hash"),
        )


def _fetch_and_store_order_events(
    settler,
    from_block: int,
    to_block: int,
) -> tuple[int, int | None]:
    """Fetch OrderExecuted events in range and upsert into DB.

    Returns (stored_count, first_failed_block). If first_failed_block
    is not None, the caller MUST NOT advance the cursor past
    first_failed_block - 1 — the failed event must remain eligible for
    retry on the next cycle, not rely on the narrow rescan window.
    """
    raw_events = settler.events.OrderExecuted.get_logs(
        from_block=from_block,
        to_block=to_block,
    )

    events_to_store = []
    first_failed_block: int | None = None
    for ev in raw_events:
        event_data = _build_order_event_data(ev)
        enriched = _enrich_with_otoken_metadata(event_data)
        if enriched is None:
            # Enrichment failed — don't store AND report the block so
            # the caller can halt cursor advancement at this point.
            if first_failed_block is None or ev.blockNumber < first_failed_block:
                first_failed_block = ev.blockNumber
            logger.error(
                "OrderExecuted enrichment failed at block=%d tx=%s. "
                "Cursor will not advance past block=%d until retry succeeds.",
                ev.blockNumber,
                ev.transactionHash.hex(),
                ev.blockNumber - 1,
            )
            continue
        # Once we've seen a failure, don't store later events either — they
        # must wait for the failing block to clear so ordering is preserved
        # (MMs observe events in block order).
        if first_failed_block is not None:
            continue
        enriched = _enrich_with_collateral_usd(enriched)
        events_to_store.append(enriched)

    stored = _store_events(events_to_store)

    for ev_data in events_to_store:
        _notify_mm(ev_data)

    return stored, first_failed_block


def _build_order_event_data(ev) -> dict:
    """Extract a flat dict from a decoded OrderExecuted event."""
    return {
        "tx_hash": ev.transactionHash.hex(),
        "block_number": ev.blockNumber,
        "log_index": ev.logIndex,
        "user_address": ev.args.user.lower(),
        "mm_address": ev.args.mm.lower(),
        "otoken_address": ev.args.oToken.lower(),
        "amount": str(ev.args.amount),
        "premium": str(ev.args.grossPremium),
        "gross_premium": str(ev.args.grossPremium),
        "net_premium": str(ev.args.netPremium),
        "protocol_fee": str(ev.args.fee),
        "collateral": str(ev.args.collateral),
        "vault_id": ev.args.vaultId,
    }


def _fetch_and_update_delivery_events(
    settler,
    from_block: int,
    to_block: int,
) -> tuple[int, int | None]:
    """Fetch PhysicalDelivery events in range and update matching DB rows.

    Returns (updated_count, first_failed_block). Same contract as
    _fetch_and_store_order_events: a non-None first_failed_block means
    the caller MUST NOT advance the cursor past first_failed_block - 1.
    """
    try:
        delivery_event_type = settler.events.PhysicalDelivery
    except AttributeError:
        logger.info(
            "PhysicalDelivery event not in ABI "
            "(contract pending upgrade), skipping delivery indexing"
        )
        return 0, None

    delivery_events_raw = delivery_event_type.get_logs(
        from_block=from_block,
        to_block=to_block,
    )

    if not delivery_events_raw:
        return 0, None

    delivery_to_update = []
    first_failed_block: int | None = None
    for ev in delivery_events_raw:
        row = _build_delivery_event_data(ev)
        if row is None:
            if first_failed_block is None or ev.blockNumber < first_failed_block:
                first_failed_block = ev.blockNumber
            logger.error(
                "PhysicalDelivery enrichment failed at block=%d tx=%s. "
                "Cursor will not advance past block=%d until retry succeeds.",
                ev.blockNumber,
                ev.transactionHash.hex(),
                ev.blockNumber - 1,
            )
            continue
        if first_failed_block is not None:
            continue
        delivery_to_update.append(row)

    return _update_delivery_events(delivery_to_update), first_failed_block


def _build_delivery_event_data(ev) -> dict | None:
    """Extract a flat dict from a decoded PhysicalDelivery event."""
    otoken_addr = ev.args.oToken.lower()
    try:
        metadata = _load_otoken_metadata(otoken_addr)
        if metadata is None:
            logger.warning(
                "No oToken metadata available for %s (tx=%s). "
                "Skipping delivery update for this event.",
                otoken_addr,
                ev.transactionHash.hex(),
            )
            return None
        is_put = metadata["is_put"]
        asset = metadata.get("asset", "eth")
        underlying = metadata.get(
            "underlying",
            settings.wbtc_address.lower()
            if asset == "btc"
            else settings.weth_address.lower(),
        )
    except Exception:
        logger.exception(
            "Could not load oToken metadata for %s (tx=%s). "
            "Skipping delivery update for this event.",
            otoken_addr,
            ev.transactionHash.hex(),
        )
        return None

    delivered_asset = underlying if is_put else settings.usdc_address.lower()
    return {
        "user_address": ev.args.user.lower(),
        "otoken_address": otoken_addr,
        "delivered_asset": delivered_asset,
        "delivered_amount": str(ev.args.contraAmount),
        "delivery_tx_hash": ev.transactionHash.hex(),
    }


# ── getLogs catchup (used on startup and after WS disconnect) ────────


async def index_once():
    """Single indexing cycle: fetch new events from chain, store in DB.

    Two passes per cycle:
    1. Forward pass: index from last_indexed_block to safe_block.
       If any event in the batch fails enrichment, the cursor is
       clamped to that block - 1 so the next cycle retries it. A
       failure at `from_block` means no progress this cycle, which is
       the correct behaviour: correctness over throughput.
    2. Re-scan pass: re-check last RESCAN_BLOCKS for missed events.
    """
    global _pending_failure_block

    w3 = get_w3()
    current_block = w3.eth.block_number
    safe_block = current_block - CONFIRMATION_BLOCKS
    last_indexed = _get_last_indexed_block()
    from_block = last_indexed + 1

    settler = get_batch_settler()

    # --- Pass 1: forward indexing (advance the pointer) ---
    if from_block <= safe_block:
        to_block = min(from_block + BLOCK_RANGE - 1, safe_block)

        stored, order_fail = _fetch_and_store_order_events(
            settler, from_block, to_block
        )
        delivered, delivery_fail = _fetch_and_update_delivery_events(
            settler,
            from_block,
            to_block,
        )

        failures = [b for b in (order_fail, delivery_fail) if b is not None]
        if failures:
            min_failed = min(failures)
            safe_to_block = min_failed - 1
            if min_failed <= from_block:
                # First block in the batch failed — cannot make any progress.
                logger.error(
                    "Forward pass: failure at block=%d == from_block. "
                    "Cursor stays at %d. Will retry next cycle.",
                    min_failed,
                    last_indexed,
                )
            else:
                _set_last_indexed_block(safe_to_block)
                logger.warning(
                    "Forward pass partial: advanced cursor to %d "
                    "(halted before failed block %d).",
                    safe_to_block,
                    min_failed,
                )
            # Track the failure so any subscription events at ≥min_failed
            # don't silently advance past it.
            if _pending_failure_block is None or min_failed < _pending_failure_block:
                _pending_failure_block = min_failed
        else:
            _set_last_indexed_block(to_block)
            # No failures in this range → any previously pending failure
            # that sat at or below to_block has now been resolved.
            if (
                _pending_failure_block is not None
                and _pending_failure_block <= to_block
            ):
                _pending_failure_block = None

        if stored > 0:
            logger.info(
                "Indexed %d events from blocks %d-%d",
                stored,
                from_block,
                to_block,
            )
        if delivered > 0:
            logger.info(
                "Updated %d positions with physical delivery data",
                delivered,
            )

    # --- Pass 2: re-scan recent blocks to catch missed events ---
    rescan_from = max(last_indexed - RESCAN_BLOCKS, 0)
    rescan_to = min(safe_block, rescan_from + BLOCK_RANGE - 1)
    if rescan_from < rescan_to:
        try:
            rescued, _ = _fetch_and_store_order_events(
                settler,
                rescan_from,
                rescan_to,
            )
            rescued_delivery, _ = _fetch_and_update_delivery_events(
                settler,
                rescan_from,
                rescan_to,
            )
            if rescued > 0:
                logger.info(
                    "Re-scan recovered %d events from blocks %d-%d",
                    rescued,
                    rescan_from,
                    rescan_to,
                )
            if rescued_delivery > 0:
                logger.info(
                    "Re-scan updated %d delivery events",
                    rescued_delivery,
                )
        except Exception:
            logger.exception(
                "Re-scan pass failed for blocks %d-%d. "
                "Forward pass succeeded. Will retry re-scan on next cycle.",
                rescan_from,
                rescan_to,
            )


# ── eth_subscribe real-time subscription ─────────────────────────────


def _process_subscription_log(settler, log) -> None:
    """Decode a raw log from the subscription and store/update in DB.

    Determines event type by matching the first topic against
    pre-computed event signature hashes.
    """
    topics = log.get("topics", [])
    if not topics:
        return

    first_topic = bytes(topics[0]) if not isinstance(topics[0], bytes) else topics[0]

    if first_topic == _ORDER_EXECUTED_TOPIC:
        _process_order_subscription_log(settler, log)
    elif first_topic == _PHYSICAL_DELIVERY_TOPIC:
        _process_delivery_subscription_log(settler, log)
    else:
        logger.debug(
            "Ignoring unrecognized event topic %s in tx %s",
            first_topic.hex(),
            log.get("transactionHash", "unknown"),
        )


def _safe_advance_target(block: int) -> int | None:
    """Return the highest block we can set as the cursor, or None if stalled.

    Clamps against `_pending_failure_block` so a successful event at
    block B cannot advance past a known-failed earlier block.
    """
    if _pending_failure_block is not None:
        if block >= _pending_failure_block:
            return _pending_failure_block - 1
    return block


def _process_order_subscription_log(settler, log) -> None:
    """Decode and store an OrderExecuted log from the subscription."""
    global _pending_failure_block

    try:
        decoded = settler.events.OrderExecuted.process_log(log)
    except Exception:
        logger.exception("Failed to decode OrderExecuted log: %s", log)
        return

    raw_event = _build_order_event_data(decoded)
    event_data = _enrich_with_otoken_metadata(raw_event)
    if event_data is None:
        # Enrichment failed; catchup getLogs pass will retry on reconnect.
        # Record the block so later subscription events don't advance past it.
        logger.warning(
            "Subscription: skipped OrderExecuted tx=%s block=%d — "
            "missing oToken metadata. Cursor will not advance past %d.",
            raw_event["tx_hash"],
            decoded.blockNumber,
            decoded.blockNumber - 1,
        )
        if (
            _pending_failure_block is None
            or decoded.blockNumber < _pending_failure_block
        ):
            _pending_failure_block = decoded.blockNumber
        return
    event_data = _enrich_with_collateral_usd(event_data)

    try:
        _store_events([event_data])
        logger.info(
            "Subscription: indexed OrderExecuted tx=%s block=%d",
            event_data["tx_hash"],
            event_data["block_number"],
        )
    except Exception:
        logger.exception(
            "Failed to store subscription OrderExecuted event: %s",
            event_data.get("tx_hash"),
        )
        return

    _notify_mm(event_data)

    target = _safe_advance_target(decoded.blockNumber)
    if target is None or target < 0:
        return
    try:
        _set_last_indexed_block(target)
    except Exception:
        logger.warning(
            "Failed to update last_indexed_block to %d, "
            "will be corrected on next event or catchup",
            target,
        )


def _process_delivery_subscription_log(settler, log) -> None:
    """Decode and update a PhysicalDelivery log from the subscription."""
    global _pending_failure_block

    try:
        decoded = settler.events.PhysicalDelivery.process_log(log)
    except Exception:
        logger.exception("Failed to decode PhysicalDelivery log: %s", log)
        return

    row = _build_delivery_event_data(decoded)
    if row is None:
        logger.warning(
            "Subscription: skipped PhysicalDelivery tx=%s block=%d — "
            "missing oToken metadata. Cursor will not advance past %d.",
            decoded.transactionHash.hex(),
            decoded.blockNumber,
            decoded.blockNumber - 1,
        )
        if (
            _pending_failure_block is None
            or decoded.blockNumber < _pending_failure_block
        ):
            _pending_failure_block = decoded.blockNumber
        return

    try:
        updated = _update_delivery_events([row])
        if updated > 0:
            logger.info(
                "Subscription: updated %d delivery rows tx=%s",
                updated,
                row["delivery_tx_hash"],
            )
    except Exception:
        logger.exception(
            "Failed to update subscription PhysicalDelivery event: %s",
            row.get("delivery_tx_hash"),
        )
        return

    target = _safe_advance_target(decoded.blockNumber)
    if target is None or target < 0:
        return
    try:
        _set_last_indexed_block(target)
    except Exception:
        logger.warning(
            "Failed to update last_indexed_block to %d, "
            "will be corrected on next event or catchup",
            target,
        )


async def _subscription_loop() -> None:
    """Connect via WebSocket, subscribe to BatchSettler logs, process events.

    On disconnect or error, catches up via getLogs and reconnects with
    exponential backoff (capped at MAX_RECONNECT_DELAY seconds).
    """
    wss_url = settings.wss_rpc_url
    settler_address = settings.batch_settler_address
    settler = get_batch_settler()
    backoff = 1

    while True:
        # Catchup: process any blocks missed while disconnected
        try:
            await index_once()
        except Exception:
            logger.exception(
                "getLogs catchup failed before subscribe. "
                "Events between last_indexed_block and now may be missed "
                "until the next reconnect catchup cycle."
            )

        try:
            async with AsyncWeb3(WebSocketProvider(wss_url)) as ws_w3:
                sub_id = await ws_w3.eth.subscribe(
                    "logs",
                    {"address": settler_address},
                )
                logger.info(
                    "Subscribed to BatchSettler logs (sub=%s)",
                    sub_id,
                )
                backoff = 1  # reset on successful connection

                async for payload in ws_w3.socket.process_subscriptions():
                    try:
                        log = payload["result"]
                    except (KeyError, TypeError):
                        logger.warning(
                            "Unexpected subscription payload: %s",
                            payload,
                        )
                        continue
                    try:
                        _process_subscription_log(settler, log)
                    except Exception:
                        logger.exception(
                            "Failed to process subscription log: %s",
                            log.get("transactionHash", "unknown"),
                        )

        except asyncio.CancelledError:
            logger.info("Subscription loop cancelled, shutting down")
            return
        except Exception:
            logger.exception(
                "WebSocket subscription error, reconnecting in %ds",
                backoff,
            )

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_RECONNECT_DELAY)


# ── Entry points ─────────────────────────────────────────────────────


async def run():
    """Start the event indexer.

    Uses eth_subscribe (WebSocket) when wss_rpc_url is configured.
    Falls back to getLogs polling otherwise.
    """
    if settings.wss_rpc_url:
        logger.info("Event indexer starting in subscription mode (WSS)")
        await _subscription_loop()
    else:
        logger.info(
            "Event indexer starting in polling mode "
            "(interval=%ds, set WSS_RPC_URL for real-time)",
            settings.event_poll_interval_seconds,
        )
        while True:
            try:
                await index_once()
            except Exception:
                logger.exception("Event indexing failed")
            await asyncio.sleep(settings.event_poll_interval_seconds)
