"""
Circuit Breaker Bot

Monitors ETH price. When the circuit breaker trips (>2% move),
calls BatchSettler.incrementMakerNonce() to invalidate on-chain
quotes signed by the operator, and deactivates ALL DB quotes
(all MMs) as a server-side safety net.
"""
import asyncio
import logging
import time

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

_last_invalidated_quote_marker: str | None = None


def _active_quote_marker() -> str | None:
    """Return a stable marker for the newest active quote, or None if none exist.

    Raises on DB failure so callers can distinguish "no active quotes"
    (safe to skip) from "could not inspect DB" (must fall back to
    unconditional on-chain invalidation).
    """
    now_ts = int(time.time())
    client = get_client()
    result = (
        client.table("mm_quotes")
        .select("id,created_at")
        .eq("is_active", True)
        .gt("deadline", now_ts)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data is None:
        raise RuntimeError("mm_quotes query returned data=None")
    if not result.data:
        return None
    row = result.data[0]
    return f"{row['id']}:{row['created_at']}"


def _deactivate_active_quotes() -> int:
    client = get_client()
    result = (
        client.table("mm_quotes")
        .update({"is_active": False})
        .eq("is_active", True)
        .execute()
    )
    return len(result.data) if result.data else 0


async def invalidate_quotes():
    """Invalidate all quotes: increment on-chain makerNonce + deactivate DB quotes."""
    global _last_invalidated_quote_marker

    try:
        marker = _active_quote_marker()
        db_lookup_failed = False
    except Exception:
        logger.exception(
            "Circuit breaker: failed to inspect active quotes. "
            "Proceeding with on-chain invalidation for safety."
        )
        marker = None
        db_lookup_failed = True

    if not db_lookup_failed and marker is None:
        logger.warning(
            "Circuit breaker tripped but found no active non-expired quotes; "
            "skipping on-chain makerNonce increment."
        )
        return

    should_send_tx = db_lookup_failed or marker != _last_invalidated_quote_marker
    if should_send_tx:
        account = get_operator_account()
        # 1. On-chain: increment makerNonce (MUST succeed for safety)
        try:
            settler = get_batch_settler()
            tx_fn = settler.functions.incrementMakerNonce()
            tx_hash = await asyncio.to_thread(
                build_and_send_tx,
                tx_fn,
                account,
                120,
                "Circuit breaker incrementMakerNonce",
            )
            # Only persist the marker when we know what it was.
            # On DB failure we re-send on every trip until the DB recovers.
            if marker is not None:
                _last_invalidated_quote_marker = marker
            logger.warning(
                f"Circuit breaker: incremented makerNonce on-chain, tx: {tx_hash}"
            )
        except Exception:
            logger.exception(
                "CRITICAL: Circuit breaker failed to increment makerNonce on-chain. "
                "Signed quotes remain valid. Will retry on next cycle."
            )
            raise
    else:
        logger.warning(
            "Circuit breaker: active quote set already invalidated on-chain; "
            "retrying DB deactivation without another makerNonce tx."
        )

    # 2. Off-chain: deactivate ALL active DB quotes (all MMs, not just operator)
    try:
        deactivated = _deactivate_active_quotes()
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
