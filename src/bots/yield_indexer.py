"""
Yield Event Indexer

Indexes CollateralDeposited and VaultSettled events from the Controller,
and YieldHarvested events from the MarginPool. These power the yield
attribution system that distributes Aave yield pro-rata to positions.

Uses the same WSS + getLogs pattern as event_indexer.py.
"""

import asyncio
import logging
from datetime import datetime, timezone

from web3 import AsyncWeb3, Web3, WebSocketProvider

from src.config import settings
from src.contracts.web3_client import (
    get_controller_yield,
    get_margin_pool,
    get_w3,
)
from src.db.database import get_client

logger = logging.getLogger(__name__)

BLOCK_RANGE = 2000
CONFIRMATION_BLOCKS = 2
RESCAN_BLOCKS = 50
MAX_RECONNECT_DELAY = 60

_YIELD_STATE_TABLE = "yield_indexer_state"

_COLLATERAL_DEPOSITED_TOPIC = Web3.keccak(
    text="CollateralDeposited(address,uint256,address,uint256)"
)
_VAULT_SETTLED_TOPIC = Web3.keccak(text="VaultSettled(address,uint256,uint256)")
_YIELD_HARVESTED_TOPIC = Web3.keccak(text="YieldHarvested(address,address,uint256)")


def _get_last_indexed_block() -> int:
    client = get_client()
    result = (
        client.table(_YIELD_STATE_TABLE)
        .select("last_indexed_block")
        .eq("id", 1)
        .execute()
    )
    if result.data:
        return result.data[0]["last_indexed_block"]
    return 0


def _set_last_indexed_block(block: int) -> None:
    client = get_client()
    client.table(_YIELD_STATE_TABLE).upsert(
        {"id": 1, "last_indexed_block": block}
    ).execute()


def _block_timestamp(block_number: int) -> datetime:
    w3 = get_w3()
    block = w3.eth.get_block(block_number)
    return datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)


def _asset_address_to_symbol(addr: str) -> str:
    """Map on-chain asset address to symbol used in our DB."""
    normalized = addr.lower()
    if normalized == settings.usdc_address.lower():
        return "usdc"
    if normalized == settings.weth_address.lower():
        return "eth"
    if normalized == settings.wbtc_address.lower():
        return "btc"
    logger.warning("Unknown asset address %s — storing raw address", addr)
    return normalized


def _process_collateral_deposited(log: dict) -> None:
    """Handle CollateralDeposited(owner, vaultId, asset, amount)."""
    controller = get_controller_yield()
    ev = controller.events.CollateralDeposited().process_log(log)
    owner = ev.args.owner.lower()
    vault_id = ev.args.vaultId
    asset_addr = ev.args.asset
    amount = ev.args.amount
    block_num = log["blockNumber"]
    tx_hash = log["transactionHash"].hex()

    asset = _asset_address_to_symbol(asset_addr)
    deposited_at = _block_timestamp(block_num)

    client = get_client()
    client.table("yield_positions").upsert(
        {
            "user_address": owner,
            "vault_id": vault_id,
            "asset": asset,
            "collateral_amount": amount,
            "deposited_at": deposited_at.isoformat(),
            "block_number": block_num,
            "tx_hash": tx_hash,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        },
        on_conflict="user_address,vault_id,asset,tx_hash",
    ).execute()

    logger.info(
        "Indexed CollateralDeposited: owner=%s vault=%d asset=%s amount=%d block=%d",
        owner[:10],
        vault_id,
        asset,
        amount,
        block_num,
    )


def _process_vault_settled(log: dict) -> None:
    """Handle VaultSettled(owner, vaultId, collateralReturned)."""
    controller = get_controller_yield()
    ev = controller.events.VaultSettled().process_log(log)
    owner = ev.args.owner.lower()
    vault_id = ev.args.vaultId
    block_num = log["blockNumber"]

    settled_at = _block_timestamp(block_num)

    client = get_client()
    client.table("yield_positions").update(
        {
            "settled_at": settled_at.isoformat(),
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    ).eq("user_address", owner).eq("vault_id", vault_id).is_(
        "settled_at", "null"
    ).execute()

    logger.info(
        "Indexed VaultSettled: owner=%s vault=%d block=%d",
        owner[:10],
        vault_id,
        block_num,
    )


def _process_yield_harvested(log: dict) -> None:
    """Handle YieldHarvested(asset, recipient, yield)."""
    margin_pool = get_margin_pool()
    ev = margin_pool.events.YieldHarvested().process_log(log)
    asset_addr = ev.args.asset
    yield_amount = getattr(ev.args, "yield")
    tx_hash = log["transactionHash"].hex()
    block_num = log["blockNumber"]

    asset = _asset_address_to_symbol(asset_addr)
    harvested_at = _block_timestamp(block_num)

    client = get_client()
    client.table("yield_distributions").upsert(
        {
            "harvest_tx_hash": tx_hash,
            "asset": asset,
            "total_yield": yield_amount,
            "period_start": harvested_at.isoformat(),
            "period_end": harvested_at.isoformat(),
            "distributed_at": harvested_at.isoformat(),
        },
        on_conflict="harvest_tx_hash",
    ).execute()

    logger.info(
        "Indexed YieldHarvested: asset=%s yield=%d tx=%s",
        asset,
        yield_amount,
        tx_hash[:16],
    )


def _process_log(log: dict) -> None:
    """Route a log entry to the appropriate handler."""
    topic = log["topics"][0]
    if topic == _COLLATERAL_DEPOSITED_TOPIC:
        _process_collateral_deposited(log)
    elif topic == _VAULT_SETTLED_TOPIC:
        _process_vault_settled(log)
    elif topic == _YIELD_HARVESTED_TOPIC:
        _process_yield_harvested(log)


def _fetch_and_process_logs(from_block: int, to_block: int) -> int:
    """Fetch logs from both contracts, process them. Returns count."""
    w3 = get_w3()
    controller_addr = Web3.to_checksum_address(settings.controller_address)
    pool_addr = Web3.to_checksum_address(settings.margin_pool_address)

    controller_logs = w3.eth.get_logs(
        {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": controller_addr,
            "topics": [
                [
                    _COLLATERAL_DEPOSITED_TOPIC.hex(),
                    _VAULT_SETTLED_TOPIC.hex(),
                ]
            ],
        }
    )

    pool_logs = w3.eth.get_logs(
        {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": pool_addr,
            "topics": [[_YIELD_HARVESTED_TOPIC.hex()]],
        }
    )

    all_logs = sorted(
        list(controller_logs) + list(pool_logs),
        key=lambda x: (x["blockNumber"], x["logIndex"]),
    )

    for log_entry in all_logs:
        try:
            _process_log(log_entry)
        except Exception:
            logger.exception(
                "Failed to process yield log at block %d", log_entry["blockNumber"]
            )

    return len(all_logs)


async def _subscribe_wss() -> None:
    """Subscribe to yield events via WebSocket for real-time indexing."""
    controller_addr = Web3.to_checksum_address(settings.controller_address)
    pool_addr = Web3.to_checksum_address(settings.margin_pool_address)
    delay = 1

    while True:
        try:
            async with AsyncWeb3(WebSocketProvider(settings.wss_rpc_url)) as w3:
                controller_sub = await w3.eth.subscribe(
                    "logs",
                    {
                        "address": controller_addr,
                        "topics": [
                            [
                                _COLLATERAL_DEPOSITED_TOPIC.hex(),
                                _VAULT_SETTLED_TOPIC.hex(),
                            ]
                        ],
                    },
                )
                logger.info(
                    "Yield indexer subscribed to Controller logs (sub=%s)",
                    controller_sub,
                )

                pool_sub = await w3.eth.subscribe(
                    "logs",
                    {
                        "address": pool_addr,
                        "topics": [[_YIELD_HARVESTED_TOPIC.hex()]],
                    },
                )
                logger.info(
                    "Yield indexer subscribed to MarginPool logs (sub=%s)",
                    pool_sub,
                )

                delay = 1
                async for msg in w3.socket.process_subscriptions():
                    try:
                        _process_log(msg["result"])
                        block_num = msg["result"]["blockNumber"]
                        _set_last_indexed_block(block_num)
                    except Exception:
                        logger.exception("Failed to process WSS yield log")

        except Exception:
            logger.exception("Yield WSS connection failed, reconnecting in %ds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)


async def run() -> None:
    """Main entry point: catchup via getLogs, then subscribe via WSS."""
    if not settings.controller_address or not settings.margin_pool_address:
        logger.warning(
            "controller_address or margin_pool_address not configured, "
            "yield indexer disabled"
        )
        return

    w3 = get_w3()
    last_block = _get_last_indexed_block()
    current_block = w3.eth.block_number - CONFIRMATION_BLOCKS

    if last_block > 0:
        start = max(last_block - RESCAN_BLOCKS, 0)
    else:
        start = current_block - RESCAN_BLOCKS

    logger.info("Yield indexer catchup: blocks %d → %d", start, current_block)

    while start <= current_block:
        end = min(start + BLOCK_RANGE - 1, current_block)
        count = _fetch_and_process_logs(start, end)
        if count > 0:
            logger.info(
                "Yield indexer processed %d logs in blocks %d–%d",
                count,
                start,
                end,
            )
        _set_last_indexed_block(end)
        start = end + 1

    if settings.wss_rpc_url:
        await _subscribe_wss()
    else:
        logger.info("No WSS URL configured, yield indexer running in poll mode")
        while True:
            await asyncio.sleep(settings.event_poll_interval_seconds)
            try:
                new_block = w3.eth.block_number - CONFIRMATION_BLOCKS
                if new_block > current_block:
                    _fetch_and_process_logs(current_block + 1, new_block)
                    _set_last_indexed_block(new_block)
                    current_block = new_block
            except Exception:
                logger.exception("Yield poll cycle failed, retrying next interval")
