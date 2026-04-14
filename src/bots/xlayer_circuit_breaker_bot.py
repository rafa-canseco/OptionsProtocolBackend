"""XLayer Circuit Breaker Bot.

Copy of circuit_breaker_bot.py targeting XLayer testnet contracts.
"""

import asyncio
import logging

from src.config import settings
from src.db.database import get_client
from src.pricing.assets import get_xlayer_assets
from src.pricing.chainlink import get_asset_price
from src.pricing.circuit_breaker import circuit_breaker
from src.contracts.web3_client import (
    get_xlayer_batch_settler,
    get_operator_account,
    build_and_send_xlayer_tx,
)

logger = logging.getLogger(__name__)


async def invalidate_quotes(asset: str):
    account = get_operator_account()

    try:
        settler = get_xlayer_batch_settler()
        tx_fn = settler.functions.incrementMakerNonce()
        tx_hash = await asyncio.to_thread(build_and_send_xlayer_tx, tx_fn, account)
        logger.warning(
            "XLayer circuit breaker (%s): incremented makerNonce, tx: %s",
            asset,
            tx_hash,
        )
    except Exception:
        logger.exception(
            "CRITICAL: XLayer circuit breaker (%s) failed to increment makerNonce.",
            asset,
        )
        raise

    client = get_client()
    result = (
        client.table("mm_quotes")
        .update({"is_active": False})
        .eq("is_active", True)
        .eq("chain", "xlayer")
        .execute()
    )
    deactivated = len(result.data) if result.data else 0
    logger.warning(
        "XLayer circuit breaker (%s): deactivated %d DB quotes",
        asset,
        deactivated,
    )


async def check_once():
    for asset in get_xlayer_assets():
        try:
            price, _ = get_asset_price(asset)
        except Exception:
            logger.exception(
                "XLayer circuit breaker: failed to read %s price",
                asset.value,
            )
            continue

        if circuit_breaker.check(price, asset.value):
            reason = circuit_breaker.pause_reason_for(asset.value)
            logger.warning("XLayer circuit breaker tripped: %s", reason)
            await invalidate_quotes(asset.value)
            circuit_breaker.update_reference(price, asset.value)


async def run():
    logger.info(
        "XLayer circuit breaker bot starting (interval=%ds)",
        settings.circuit_breaker_poll_seconds,
    )
    while True:
        try:
            await check_once()
        except Exception:
            logger.exception("XLayer circuit breaker check failed")
        await asyncio.sleep(settings.circuit_breaker_poll_seconds)
