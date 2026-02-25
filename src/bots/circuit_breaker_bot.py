"""
Circuit Breaker Bot

Monitors ETH price. When the circuit breaker trips (>2% move),
calls BatchSettler.incrementMakerNonce() to invalidate on-chain
quotes signed by the operator, and deactivates ALL DB quotes
(all MMs) as a server-side safety net.
"""
import asyncio
import logging

from src.config import settings
from src.db.database import get_client
from src.pricing.chainlink import get_eth_price
from src.pricing.circuit_breaker import circuit_breaker
from src.contracts.web3_client import (
    get_batch_settler,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)


async def invalidate_quotes():
    """Invalidate all quotes: increment on-chain makerNonce + deactivate DB quotes."""
    account = get_operator_account()

    # 1. On-chain: increment makerNonce (MUST succeed for safety)
    try:
        settler = get_batch_settler()
        tx_fn = settler.functions.incrementMakerNonce()
        tx_hash = await asyncio.to_thread(build_and_send_tx, tx_fn, account)
        logger.warning(
            f"Circuit breaker: incremented makerNonce on-chain, tx: {tx_hash}"
        )
    except Exception:
        logger.exception(
            "CRITICAL: Circuit breaker failed to increment makerNonce on-chain. "
            "Signed quotes remain valid. Will retry on next cycle."
        )
        raise

    # 2. Off-chain: deactivate ALL active DB quotes (all MMs, not just operator)
    try:
        client = get_client()
        result = (
            client.table("mm_quotes")
            .update({"is_active": False})
            .eq("is_active", True)
            .execute()
        )
        deactivated = len(result.data) if result.data else 0
        logger.warning(
            f"Circuit breaker: deactivated {deactivated} DB quotes (all MMs)"
        )
    except Exception:
        logger.exception("Circuit breaker: failed to deactivate DB quotes")


async def check_once():
    """Single circuit breaker check. If tripped, invalidates quotes."""
    try:
        eth_price, _ = get_eth_price()
    except Exception:
        logger.exception(
            "Circuit breaker: failed to read ETH price from oracle. "
            "Safety check skipped this cycle."
        )
        return

    if circuit_breaker.check(eth_price):
        logger.warning(f"Circuit breaker tripped: {circuit_breaker.pause_reason}")
        await invalidate_quotes()  # raises if on-chain nonce increment fails
        circuit_breaker.update_reference(eth_price)


async def run():
    """Main loop: check ETH price every N seconds."""
    logger.info(f"Circuit breaker bot starting (interval={settings.circuit_breaker_poll_seconds}s)")
    while True:
        try:
            await check_once()
        except Exception:
            logger.exception("Circuit breaker check failed")
        await asyncio.sleep(settings.circuit_breaker_poll_seconds)
