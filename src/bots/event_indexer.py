"""
Event Indexer Bot

Polls OrderExecuted events from BatchSettler.executeOrder(),
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
    """Read oToken on-chain metadata for denormalization into DB."""
    try:
        ot = get_otoken(event_data["otoken_address"])
        event_data["strike_price"] = ot.functions.strikePrice().call()
        event_data["expiry"] = ot.functions.expiry().call()
        event_data["is_put"] = ot.functions.isPut().call()
    except Exception:
        logger.warning(f"Could not read oToken metadata for {event_data['otoken_address']}")
    return event_data


def _store_events(events: list[dict]) -> int:
    """Insert events into Supabase. Returns count inserted."""
    if not events:
        return 0
    client = get_client()
    result = client.table("order_events").upsert(
        events,
        on_conflict="tx_hash",
    ).execute()
    return len(result.data) if result.data else 0


async def index_once():
    """Single indexing cycle: fetch new events from chain, store in DB."""
    w3 = get_w3()
    current_block = w3.eth.block_number
    from_block = _get_last_indexed_block() + 1

    if from_block > current_block:
        return

    settler = get_batch_settler()
    to_block = min(from_block + BLOCK_RANGE - 1, current_block)

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
            "premium": str(ev.args.premium),
            "collateral": str(ev.args.collateral),
            "vault_id": ev.args.vaultId,
        }
        event_data = _enrich_with_otoken_metadata(event_data)
        events_to_store.append(event_data)

    stored = _store_events(events_to_store)
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
