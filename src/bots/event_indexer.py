"""
Event Indexer Bot

Polls OrderExecuted and PhysicalDelivery events from BatchSettler,
stores OrderExecuted events and updates existing rows with PhysicalDelivery
delivery data in the order_events Supabase table.
Tracks last_indexed_block for resumability.

Uses a re-scan window to catch events missed due to RPC load balancer
inconsistency (getLogs returning stale data on some nodes).
"""
import asyncio
import logging

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import get_batch_settler, get_otoken, get_w3

logger = logging.getLogger(__name__)

BLOCK_RANGE = 2000  # max blocks per getLogs query
CONFIRMATION_BLOCKS = 10  # wait N blocks before indexing to avoid RPC sync issues
RESCAN_BLOCKS = 50  # re-scan last N blocks each cycle to catch missed events


def _get_last_indexed_block() -> int:
    client = get_client()
    result = client.table("indexer_state").select("last_indexed_block").eq("id", 1).execute()
    if result.data:
        return result.data[0]["last_indexed_block"]
    return 0


def _set_last_indexed_block(block: int) -> None:
    client = get_client()
    client.table("indexer_state").upsert({
        "id": 1,
        "last_indexed_block": block,
    }).execute()


def _enrich_with_otoken_metadata(event_data: dict) -> dict:
    """Read oToken on-chain metadata for denormalization into DB.

    All three fields (strike_price, expiry, is_put) are assigned atomically —
    either all succeed or none are set. These fields are critical for settlement
    (identify_itm_positions depends on them).
    """
    try:
        ot = get_otoken(event_data["otoken_address"])
        strike = ot.functions.strikePrice().call()
        expiry = ot.functions.expiry().call()
        is_put = ot.functions.isPut().call()
        event_data["strike_price"] = strike
        event_data["expiry"] = expiry
        event_data["is_put"] = is_put
    except Exception:
        logger.exception(
            f"Could not read oToken metadata for {event_data['otoken_address']}. "
            f"This position will lack settlement-critical fields."
        )
    return event_data


def _store_events(events: list[dict]) -> int:
    """Insert events into Supabase. Returns count inserted.

    Uses upsert on tx_hash so re-scanned events are safely deduplicated.
    Raises if Supabase accepts the request but returns empty data for a
    non-empty input — prevents the block pointer from advancing past lost events.
    """
    if not events:
        return 0
    client = get_client()
    result = client.table("order_events").upsert(
        events,
        on_conflict="tx_hash",
    ).execute()
    if not result.data:
        logger.error(
            f"_store_events: Supabase returned empty data for {len(events)} events"
        )
        raise RuntimeError(f"Supabase upsert returned no data for {len(events)} events")
    return len(result.data)


def _update_delivery_events(delivery_events: list[dict]) -> int:
    """Update existing order_events rows with physical delivery data.

    Matches on (user_address, otoken_address). If a user has multiple positions
    for the same oToken, all will be updated — this is acceptable because all
    positions on the same oToken share the same ITM/OTM outcome.
    """
    if not delivery_events:
        return 0
    client = get_client()
    updated = 0
    for ev in delivery_events:
        result = client.table("order_events").update({
            "settlement_type": "physical",
            "delivered_asset": ev["delivered_asset"],
            "delivered_amount": ev["delivered_amount"],
            "delivery_tx_hash": ev["delivery_tx_hash"],
            "is_itm": True,
        }).eq("user_address", ev["user_address"]).eq(
            "otoken_address", ev["otoken_address"],
        ).execute()
        if result.data:
            updated += len(result.data)
        else:
            logger.warning(
                f"Physical delivery event matched no DB row: "
                f"user={ev['user_address']} otoken={ev['otoken_address']} "
                f"tx={ev['delivery_tx_hash']}"
            )
    return updated


def _fetch_and_store_order_events(settler, from_block: int, to_block: int) -> int:
    """Fetch OrderExecuted events in range and upsert into DB. Returns count stored."""
    raw_events = settler.events.OrderExecuted.get_logs(
        from_block=from_block,
        to_block=to_block,
    )

    events_to_store = []
    for ev in raw_events:
        event_data = {
            "tx_hash": ev.transactionHash.hex(),
            "block_number": ev.blockNumber,
            "log_index": ev.logIndex,
            "user_address": ev.args.user.lower(),
            "otoken_address": ev.args.oToken.lower(),
            "amount": str(ev.args.amount),
            "premium": str(ev.args.grossPremium),
            "gross_premium": str(ev.args.grossPremium),
            "net_premium": str(ev.args.netPremium),
            "protocol_fee": str(ev.args.fee),
            "collateral": str(ev.args.collateral),
            "vault_id": ev.args.vaultId,
        }
        event_data = _enrich_with_otoken_metadata(event_data)
        events_to_store.append(event_data)

    return _store_events(events_to_store)


def _fetch_and_update_delivery_events(settler, from_block: int, to_block: int) -> int:
    """Fetch PhysicalDelivery events in range and update matching DB rows."""
    try:
        delivery_event_type = settler.events.PhysicalDelivery
    except AttributeError:
        logger.info("PhysicalDelivery event not in ABI (contract pending upgrade), skipping delivery indexing")
        return 0

    delivery_events_raw = delivery_event_type.get_logs(
        from_block=from_block,
        to_block=to_block,
    )

    if not delivery_events_raw:
        return 0

    delivery_to_update = []
    for ev in delivery_events_raw:
        otoken_addr = ev.args.oToken.lower()
        try:
            ot = get_otoken(otoken_addr)
            is_put = ot.functions.isPut().call()
        except Exception:
            logger.exception(
                f"Could not read isPut() for oToken {otoken_addr} "
                f"(tx={ev.transactionHash.hex()}). "
                f"Skipping delivery update for this event."
            )
            continue
        delivered_asset = settings.weth_address.lower() if is_put else settings.usdc_address.lower()
        delivery_to_update.append({
            "user_address": ev.args.user.lower(),
            "otoken_address": otoken_addr,
            "delivered_asset": delivered_asset,
            "delivered_amount": str(ev.args.contraAmount),
            "delivery_tx_hash": ev.transactionHash.hex(),
        })

    return _update_delivery_events(delivery_to_update)


async def index_once():
    """Single indexing cycle: fetch new events from chain, store in DB.

    Two passes per cycle:
    1. Forward pass: index from last_indexed_block to safe_block (advances pointer)
    2. Re-scan pass: re-check the last RESCAN_BLOCKS to catch events missed
       by the RPC load balancer on previous cycles (upsert deduplicates safely)
    """
    w3 = get_w3()
    current_block = w3.eth.block_number
    safe_block = current_block - CONFIRMATION_BLOCKS
    last_indexed = _get_last_indexed_block()
    from_block = last_indexed + 1

    settler = get_batch_settler()

    # --- Pass 1: forward indexing (advance the pointer) ---
    if from_block <= safe_block:
        to_block = min(from_block + BLOCK_RANGE - 1, safe_block)

        stored = _fetch_and_store_order_events(settler, from_block, to_block)
        delivered = _fetch_and_update_delivery_events(settler, from_block, to_block)

        _set_last_indexed_block(to_block)

        if stored > 0:
            logger.info(f"Indexed {stored} events from blocks {from_block}-{to_block}")
        if delivered > 0:
            logger.info(f"Updated {delivered} positions with physical delivery data")

    # --- Pass 2: re-scan recent blocks to catch missed events ---
    rescan_from = max(last_indexed - RESCAN_BLOCKS, 0)
    rescan_to = min(safe_block, rescan_from + BLOCK_RANGE - 1)
    if rescan_from < rescan_to:
        try:
            rescued = _fetch_and_store_order_events(settler, rescan_from, rescan_to)
            rescued_delivery = _fetch_and_update_delivery_events(settler, rescan_from, rescan_to)
            if rescued > 0:
                logger.info(f"Re-scan recovered {rescued} events from blocks {rescan_from}-{rescan_to}")
            if rescued_delivery > 0:
                logger.info(f"Re-scan updated {rescued_delivery} delivery events")
        except Exception:
            logger.exception(
                f"Re-scan pass failed for blocks {rescan_from}-{rescan_to}. "
                f"Forward pass succeeded. Will retry re-scan on next cycle."
            )


async def run():
    """Main loop: poll for new events every N seconds."""
    logger.info(f"Event indexer starting (interval={settings.event_poll_interval_seconds}s)")
    while True:
        try:
            await index_once()
        except Exception:
            logger.exception("Event indexing failed")
        await asyncio.sleep(settings.event_poll_interval_seconds)
