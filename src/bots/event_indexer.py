"""
Event Indexer Bot

Polls OrderExecuted and PhysicalDeliveryExecuted events from BatchSettler,
stores them in the order_events Supabase table.
Tracks last_indexed_block for resumability.
"""
import asyncio
import logging

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import get_batch_settler, get_otoken, get_w3

logger = logging.getLogger(__name__)

BLOCK_RANGE = 2000  # max blocks per getLogs query


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


async def index_once():
    """Single indexing cycle: fetch new events from chain, store in DB."""
    w3 = get_w3()
    current_block = w3.eth.block_number
    from_block = _get_last_indexed_block() + 1

    if from_block > current_block:
        return

    settler = get_batch_settler()
    to_block = min(from_block + BLOCK_RANGE - 1, current_block)

    # Index OrderExecuted events (new orders)
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

    stored = _store_events(events_to_store)

    # Index PhysicalDeliveryExecuted events (ITM delivery)
    try:
        delivery_event_type = settler.events.PhysicalDeliveryExecuted
    except AttributeError:
        logger.info("PhysicalDeliveryExecuted event not in ABI (contract pending upgrade)")
        delivery_event_type = None

    if delivery_event_type is not None:
        # get_logs errors (RPC/network) must propagate to prevent block pointer advance
        delivery_events_raw = delivery_event_type.get_logs(
            from_block=from_block,
            to_block=to_block,
        )
    else:
        delivery_events_raw = []

    if delivery_events_raw:
        delivery_to_update = []
        for ev in delivery_events_raw:
            delivery_to_update.append({
                "user_address": ev.args.user.lower(),
                "otoken_address": ev.args.oToken.lower(),
                "delivered_asset": ev.args.deliveredAsset.lower(),
                "delivered_amount": str(ev.args.deliveredAmount),
                "delivery_tx_hash": ev.transactionHash.hex(),
            })
        delivered = _update_delivery_events(delivery_to_update)
        if delivered > 0:
            logger.info(f"Updated {delivered} positions with physical delivery data")

    _set_last_indexed_block(to_block)

    if stored > 0:
        logger.info(f"Indexed {stored} events from blocks {from_block}-{to_block}")


async def run():
    """Main loop: poll for new events every N seconds."""
    logger.info(f"Event indexer starting (interval={settings.event_poll_interval_seconds}s)")
    while True:
        try:
            await index_once()
        except Exception:
            logger.exception("Event indexing failed")
        await asyncio.sleep(settings.event_poll_interval_seconds)
