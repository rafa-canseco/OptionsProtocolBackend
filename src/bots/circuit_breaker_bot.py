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


def _has_active_non_expired_quotes() -> bool:
    """Return True if any active, non-expired quote exists in the DB.

    Raises on DB failure so callers can distinguish "no active quotes"
    (safe to skip) from "could not inspect DB" (must fall back to
    unconditional on-chain invalidation). Does NOT memoize any result —
    we re-check on every trip so a reorg, process restart, or external
    nonce change cannot cause a silent skip of the on-chain tx.
    """
    now_ts = int(time.time())
    client = get_client()
    result = (
        client.table("mm_quotes")
        .select("id")
        .eq("is_active", True)
        .gt("deadline", now_ts)
        .limit(1)
        .execute()
    )
    if result.data is None:
        raise RuntimeError("mm_quotes query returned data=None")
    return bool(result.data)


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
    """Invalidate all quotes: increment on-chain makerNonce + deactivate DB quotes.

    Only skips the on-chain tx when the DB affirmatively confirms zero
    active non-expired quotes. Any DB failure or presence of a single
    active quote forces the on-chain increment — we never trust
    in-memory state to decide "already invalidated" because that state
    can diverge from on-chain reality on reorg or process restart.
    """
    try:
        has_active = _has_active_non_expired_quotes()
        db_lookup_failed = False
    except Exception:
        logger.exception(
            "Circuit breaker: failed to inspect active quotes. "
            "Proceeding with on-chain invalidation for safety."
        )
        has_active = True  # treated as "must send tx"
        db_lookup_failed = True

    if not db_lookup_failed and not has_active:
        logger.warning(
            "Circuit breaker tripped but found no active non-expired quotes; "
            "skipping on-chain makerNonce increment."
        )
        return

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
