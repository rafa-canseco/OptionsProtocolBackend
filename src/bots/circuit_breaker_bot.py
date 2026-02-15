"""
Circuit Breaker Bot

Monitors ETH price. When the circuit breaker trips (>2% move),
calls PriceSheet.invalidateQuotes() to cancel all on-chain quotes.
"""
import asyncio
import logging
import time

from src.config import settings
from src.pricing.chainlink import get_eth_price
from src.pricing.circuit_breaker import circuit_breaker
from src.contracts.web3_client import (
    get_price_sheet,
    get_otoken_factory,
    get_otoken,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)


def get_active_otoken_addresses() -> list[str]:
    """Get addresses of all non-expired oTokens."""
    factory = get_otoken_factory()
    count = factory.functions.getOTokensLength().call()
    now = int(time.time())
    active = []
    for i in range(count):
        addr = factory.functions.oTokens(i).call()
        ot = get_otoken(addr)
        expiry = ot.functions.expiry().call()
        if expiry > now:
            active.append(addr)
    return active


async def invalidate_all_quotes():
    """Invalidate all active quotes on PriceSheet."""
    addresses = get_active_otoken_addresses()
    if not addresses:
        logger.info("No active oTokens to invalidate")
        return

    ps = get_price_sheet()
    account = get_operator_account()

    tx_fn = ps.functions.invalidateQuotes(addresses)
    tx_hash = build_and_send_tx(tx_fn, account)
    logger.warning(f"Circuit breaker: invalidated {len(addresses)} quotes, tx: {tx_hash}")


async def check_once():
    """Single circuit breaker check. If tripped, invalidates quotes on-chain."""
    eth_price, _ = get_eth_price()

    if circuit_breaker.check(eth_price):
        logger.warning(f"Circuit breaker tripped: {circuit_breaker.pause_reason}")
        await invalidate_all_quotes()
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
