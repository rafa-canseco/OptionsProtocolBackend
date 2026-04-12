"""
Circuit Breaker Bot

Monitors spot prices for all supported assets. When the circuit
breaker trips (>2% move) for ANY asset, calls
BatchSettler.incrementMakerNonce() to invalidate on-chain quotes
signed by the operator, and deactivates ALL DB quotes (all MMs)
as a server-side safety net.
"""

import asyncio
import logging

from src.config import settings
from src.db.database import get_client
from src.pricing.assets import get_base_assets
from src.pricing.chainlink import get_asset_price
from src.pricing.circuit_breaker import circuit_breaker
from src.contracts.web3_client import (
    get_batch_settler,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)


async def invalidate_quotes(asset: str):
    """Invalidate all quotes: increment on-chain nonce + deactivate DB quotes."""
    account = get_operator_account()

    try:
        settler = get_batch_settler()
        tx_fn = settler.functions.incrementMakerNonce()
        tx_hash = await asyncio.to_thread(build_and_send_tx, tx_fn, account)
        logger.warning(
            "Circuit breaker (%s): incremented makerNonce, tx: %s",
            asset,
            tx_hash,
        )
    except Exception:
        logger.exception(
            "CRITICAL: Circuit breaker (%s) failed to increment "
            "makerNonce. Signed quotes remain valid.",
            asset,
        )
        raise

    client = get_client()
    result = (
        client.table("mm_quotes")
        .update({"is_active": False})
        .eq("is_active", True)
        .eq("chain", "base")
        .execute()
    )
    deactivated = len(result.data) if result.data else 0
    logger.warning(
        "Circuit breaker (%s): deactivated %d DB quotes",
        asset,
        deactivated,
    )


async def check_once():
    """Check all assets. If any trips, invalidate quotes."""
    for asset in get_base_assets():
        try:
            price, _ = get_asset_price(asset)
        except Exception:
            logger.exception(
                "Circuit breaker: failed to read %s price. "
                "Safety check skipped for this asset.",
                asset.value,
            )
            continue

        if circuit_breaker.check(price, asset.value):
            reason = circuit_breaker.pause_reason_for(asset.value)
            logger.warning("Circuit breaker tripped: %s", reason)
            await invalidate_quotes(asset.value)
            circuit_breaker.update_reference(price, asset.value)


async def run():
    """Main loop: check prices every N seconds."""
    logger.info(
        "Circuit breaker bot starting (interval=%ds)",
        settings.circuit_breaker_poll_seconds,
    )
    while True:
        try:
            await check_once()
        except Exception:
            logger.exception("Circuit breaker check failed")
        await asyncio.sleep(settings.circuit_breaker_poll_seconds)
