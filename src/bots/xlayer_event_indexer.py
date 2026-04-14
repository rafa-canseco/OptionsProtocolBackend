"""XLayer Event Indexer Bot.

Copy of event_indexer.py targeting XLayer testnet contracts.
Indexes OrderExecuted and PhysicalDelivery events from the
XLayer BatchSettler into order_events.
"""

import asyncio
import logging

from web3 import AsyncWeb3, Web3, WebSocketProvider

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import (
    get_xlayer_batch_settler,
    get_xlayer_otoken,
    get_xlayer_w3,
)
from src.api.mm_ws import notify_mm_fill
from src.pricing.chainlink import get_asset_price
from src.pricing.assets import Asset
from src.pricing.utils import collateral_to_usd

logger = logging.getLogger(__name__)

BLOCK_RANGE = 2000
CONFIRMATION_BLOCKS = 2
RESCAN_BLOCKS = 50
MAX_RECONNECT_DELAY = 60

# Use same topic hashes (event signatures are chain-agnostic)
_ORDER_EXECUTED_TOPIC = Web3.keccak(
    text=(
        "OrderExecuted(address,address,address,"
        "uint256,uint256,uint256,uint256,uint256,uint256)"
    )
)
_PHYSICAL_DELIVERY_TOPIC = Web3.keccak(
    text="PhysicalDelivery(address,address,uint256,uint256)"
)

# Separate indexer_state row for XLayer (id=2)
_INDEXER_STATE_ID = 2


def _get_last_indexed_block() -> int:
    client = get_client()
    result = (
        client.table("indexer_state")
        .select("last_indexed_block")
        .eq("id", _INDEXER_STATE_ID)
        .execute()
    )
    if result.data:
        return result.data[0]["last_indexed_block"]
    return 0


def _set_last_indexed_block(block: int) -> None:
    client = get_client()
    client.table("indexer_state").upsert(
        {
            "id": _INDEXER_STATE_ID,
            "last_indexed_block": block,
        }
    ).execute()


def _underlying_to_asset(underlying_addr: str) -> str:
    addr = underlying_addr.lower()
    wokb = settings.wokb_address.lower()
    if addr == wokb:
        return "okb"
    return "unknown"


def _enrich_with_otoken_metadata(event_data: dict) -> dict:
    try:
        ot = get_xlayer_otoken(event_data["otoken_address"])
        strike = ot.functions.strikePrice().call()
        expiry = ot.functions.expiry().call()
        is_put = ot.functions.isPut().call()
        underlying = ot.functions.underlying().call()
        event_data["strike_price"] = strike
        event_data["expiry"] = expiry
        event_data["is_put"] = is_put
        event_data["asset"] = _underlying_to_asset(underlying)
    except Exception:
        logger.exception(
            "Could not read oToken metadata for %s",
            event_data["otoken_address"],
        )
    return event_data


def _enrich_with_collateral_usd(event_data: dict) -> dict:
    is_put = event_data.get("is_put")

    if is_put is True or is_put is None:
        event_data["collateral_usd"] = collateral_to_usd(event_data, 0.0, 0.0)
        return event_data

    try:
        okb_spot, _ = get_asset_price(Asset.OKB)
        event_data["collateral_usd"] = collateral_to_usd(event_data, okb_spot, 0.0)
    except Exception:
        logger.warning(
            "Could not fetch OKB spot for CALL tx=%s",
            event_data.get("tx_hash"),
        )
        event_data["collateral_usd"] = None
    return event_data


def _store_events(events: list[dict]) -> int:
    if not events:
        return 0
    client = get_client()
    result = (
        client.table("order_events").upsert(events, on_conflict="tx_hash").execute()
    )
    if not result.data:
        raise RuntimeError(f"Supabase upsert returned no data for {len(events)} events")
    return len(result.data)


def _update_delivery_events(delivery_events: list[dict]) -> int:
    if not delivery_events:
        return 0
    client = get_client()
    updated = 0
    for ev in delivery_events:
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
            .eq("user_address", ev["user_address"])
            .eq("otoken_address", ev["otoken_address"])
            .execute()
        )
        if result.data:
            updated += len(result.data)
    return updated


def _notify_mm(event_data: dict) -> None:
    mm_addr = event_data.get("mm_address")
    if not mm_addr:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(notify_mm_fill(mm_addr, event_data))
    except RuntimeError:
        pass


def _build_order_event_data(ev) -> dict:
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


def _fetch_and_store_order_events(settler, from_block, to_block) -> int:
    raw_events = settler.events.OrderExecuted.get_logs(
        from_block=from_block,
        to_block=to_block,
    )
    events_to_store = []
    for ev in raw_events:
        event_data = _build_order_event_data(ev)
        event_data = _enrich_with_otoken_metadata(event_data)
        event_data = _enrich_with_collateral_usd(event_data)
        events_to_store.append(event_data)

    stored = _store_events(events_to_store)
    for ev_data in events_to_store:
        _notify_mm(ev_data)
    return stored


def _build_delivery_event_data(ev) -> dict | None:
    otoken_addr = ev.args.oToken.lower()
    try:
        ot = get_xlayer_otoken(otoken_addr)
        is_put = ot.functions.isPut().call()
    except Exception:
        logger.exception("Could not read isPut() for oToken %s", otoken_addr)
        return None

    delivered_asset = (
        settings.wokb_address.lower()
        if is_put
        else settings.xlayer_usdc_address.lower()
    )
    return {
        "user_address": ev.args.user.lower(),
        "otoken_address": otoken_addr,
        "delivered_asset": delivered_asset,
        "delivered_amount": str(ev.args.contraAmount),
        "delivery_tx_hash": ev.transactionHash.hex(),
    }


def _fetch_and_update_delivery_events(settler, from_block, to_block) -> int:
    try:
        delivery_event_type = settler.events.PhysicalDelivery
    except AttributeError:
        return 0

    delivery_events_raw = delivery_event_type.get_logs(
        from_block=from_block,
        to_block=to_block,
    )
    if not delivery_events_raw:
        return 0

    delivery_to_update = []
    for ev in delivery_events_raw:
        row = _build_delivery_event_data(ev)
        if row:
            delivery_to_update.append(row)
    return _update_delivery_events(delivery_to_update)


async def index_once():
    w3 = get_xlayer_w3()
    current_block = w3.eth.block_number
    safe_block = current_block - CONFIRMATION_BLOCKS
    last_indexed = _get_last_indexed_block()
    from_block = last_indexed + 1

    settler = get_xlayer_batch_settler()

    if from_block <= safe_block:
        to_block = min(from_block + BLOCK_RANGE - 1, safe_block)
        stored = _fetch_and_store_order_events(settler, from_block, to_block)
        delivered = _fetch_and_update_delivery_events(settler, from_block, to_block)
        _set_last_indexed_block(to_block)
        if stored > 0:
            logger.info(
                "XLayer indexed %d events blocks %d-%d",
                stored,
                from_block,
                to_block,
            )
        if delivered > 0:
            logger.info("XLayer updated %d delivery events", delivered)

    rescan_from = max(last_indexed - RESCAN_BLOCKS, 0)
    rescan_to = min(safe_block, rescan_from + BLOCK_RANGE - 1)
    if rescan_from < rescan_to:
        try:
            _fetch_and_store_order_events(settler, rescan_from, rescan_to)
            _fetch_and_update_delivery_events(settler, rescan_from, rescan_to)
        except Exception:
            logger.exception(
                "XLayer re-scan failed for blocks %d-%d",
                rescan_from,
                rescan_to,
            )


async def _subscription_loop() -> None:
    wss_url = settings.xlayer_wss_rpc_url
    settler_address = settings.xlayer_batch_settler_address
    settler = get_xlayer_batch_settler()
    backoff = 1

    while True:
        try:
            await index_once()
        except Exception:
            logger.exception("XLayer getLogs catchup failed")

        try:
            async with AsyncWeb3(WebSocketProvider(wss_url)) as ws_w3:
                sub_id = await ws_w3.eth.subscribe(
                    "logs",
                    {"address": settler_address},
                )
                logger.info(
                    "XLayer subscribed to BatchSettler logs (sub=%s)",
                    sub_id,
                )
                backoff = 1

                async for payload in ws_w3.socket.process_subscriptions():
                    try:
                        log = payload["result"]
                    except (KeyError, TypeError):
                        continue
                    try:
                        topics = log.get("topics", [])
                        if not topics:
                            continue
                        first = (
                            bytes(topics[0])
                            if not isinstance(topics[0], bytes)
                            else topics[0]
                        )
                        if first == _ORDER_EXECUTED_TOPIC:
                            decoded = settler.events.OrderExecuted.process_log(log)
                            event_data = _build_order_event_data(decoded)
                            event_data = _enrich_with_otoken_metadata(event_data)
                            event_data = _enrich_with_collateral_usd(event_data)
                            _store_events([event_data])
                            _notify_mm(event_data)
                            _set_last_indexed_block(decoded.blockNumber)
                        elif first == _PHYSICAL_DELIVERY_TOPIC:
                            decoded = settler.events.PhysicalDelivery.process_log(log)
                            row = _build_delivery_event_data(decoded)
                            if row:
                                _update_delivery_events([row])
                            _set_last_indexed_block(decoded.blockNumber)
                    except Exception:
                        logger.exception("Failed to process XLayer sub log")

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("XLayer WS error, reconnecting in %ds", backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_RECONNECT_DELAY)


async def run():
    if settings.xlayer_wss_rpc_url:
        logger.info("XLayer event indexer starting (WSS mode)")
        await _subscription_loop()
    else:
        logger.info(
            "XLayer event indexer starting (polling, interval=%ds)",
            settings.event_poll_interval_seconds,
        )
        while True:
            try:
                await index_once()
            except Exception:
                logger.exception("XLayer event indexing failed")
            await asyncio.sleep(settings.event_poll_interval_seconds)
