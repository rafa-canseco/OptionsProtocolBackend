"""
Expiry Settler Bot

At 08:00 UTC daily, settles all expired vaults via
BatchSettler.batchSettleVaults().
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import (
    get_batch_settler,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 50  # max vaults per tx to avoid gas limit


def get_expired_unsettled() -> list[dict]:
    """Get all unsettled positions with expired oTokens."""
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select("user_address, vault_id, otoken_address, expiry")
        .eq("is_settled", False)
        .lt("expiry", now)
        .execute()
    )
    return result.data or []


def _mark_settled(user_vaults: list[tuple[str, int]], settlement_tx: str) -> None:
    """Mark positions as settled in the DB."""
    client = get_client()
    for user_addr, vault_id in user_vaults:
        client.table("order_events").update({
            "is_settled": True,
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "settlement_tx_hash": settlement_tx,
        }).eq("user_address", user_addr).eq("vault_id", vault_id).execute()


async def settle_once():
    """Single settlement cycle."""
    positions = get_expired_unsettled()
    if not positions:
        logger.info("No expired positions to settle")
        return

    for i in range(0, len(positions), MAX_BATCH_SIZE):
        batch = positions[i:i + MAX_BATCH_SIZE]
        owners = [p["user_address"] for p in batch]
        vault_ids = [p["vault_id"] for p in batch]

        settler = get_batch_settler()
        account = get_operator_account()

        try:
            tx_fn = settler.functions.batchSettleVaults(owners, vault_ids)
            tx_hash = build_and_send_tx(tx_fn, account)
            logger.info(f"Settled {len(batch)} vaults, tx: {tx_hash}")
            _mark_settled(list(zip(owners, vault_ids)), tx_hash)
        except Exception:
            logger.exception(f"Settlement batch failed for {len(batch)} vaults")


async def _wait_until_target_hour():
    """Sleep until the next 08:00 UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(
        hour=settings.expiry_settle_hour_utc,
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)

    wait_seconds = (target - now).total_seconds()
    logger.info(f"Expiry settler waiting {wait_seconds:.0f}s until {target.isoformat()}")
    await asyncio.sleep(wait_seconds)


async def run():
    """Main loop: settle at 08:00 UTC daily."""
    logger.info("Expiry settler starting")
    while True:
        await _wait_until_target_hour()
        try:
            await settle_once()
        except Exception:
            logger.exception("Expiry settlement failed")
