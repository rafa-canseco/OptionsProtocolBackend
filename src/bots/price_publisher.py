"""
Price Publisher Bot

Reads the BS price engine, maps to on-chain oTokens,
and calls PriceSheet.publishQuotes() periodically.
"""
import asyncio
import logging
import time

from src.config import settings
from src.pricing.chainlink import get_eth_price
from src.pricing.deribit import get_eth_iv
from src.pricing.price_sheet import generate_price_sheet
from src.contracts.web3_client import (
    get_price_sheet,
    get_otoken_factory,
    get_otoken,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)

SPREAD = 0.01  # 1% bid-ask spread
USDC_DECIMALS = 6


def premium_to_usdc(premium_usd: float) -> int:
    """Convert a USD premium float to USDC 6-decimal integer."""
    return max(int(premium_usd * 10**USDC_DECIMALS), 1)


def discover_active_otokens() -> list[dict]:
    """Query OTokenFactory to find all non-expired oTokens and their metadata."""
    factory = get_otoken_factory()
    count = factory.functions.getOTokensLength().call()
    now = int(time.time())
    active = []
    for i in range(count):
        addr = factory.functions.oTokens(i).call()
        ot = get_otoken(addr)
        expiry = ot.functions.expiry().call()
        if expiry <= now:
            continue
        strike = ot.functions.strikePrice().call()
        is_put = ot.functions.isPut().call()
        active.append({
            "address": addr,
            "strike_price": strike,        # 8 decimals
            "expiry": expiry,
            "is_put": is_put,
            "strike_usd": strike / 10**8,
            "expiry_days": (expiry - now) / 86400,
        })
    return active


def match_quotes_to_otokens(quotes, otokens):
    """Match BS-generated quotes to on-chain oToken addresses.

    Returns list of (otoken_address, bid_usdc, ask_usdc, deadline).
    """
    matched = []
    for ot in otokens:
        ot_type = "put" if ot["is_put"] else "call"
        best_quote = None
        best_expiry_diff = float("inf")
        for q in quotes:
            if q.option_type.value != ot_type:
                continue
            if abs(q.strike - ot["strike_usd"]) > 1.0:
                continue
            diff = abs(q.expiry_days - ot["expiry_days"])
            if diff < best_expiry_diff:
                best_expiry_diff = diff
                best_quote = q

        if best_quote is None:
            continue

        bid = premium_to_usdc(best_quote.premium * (1 - SPREAD))
        ask = premium_to_usdc(best_quote.premium * (1 + SPREAD))
        deadline = int(time.time()) + settings.quote_deadline_seconds
        matched.append((ot["address"], bid, ask, deadline))

    return matched


async def publish_once():
    """Single publish cycle: generate prices, match to oTokens, publish on-chain."""
    eth_price, _ = get_eth_price()
    iv = await get_eth_iv()
    quotes = generate_price_sheet(spot=eth_price, iv=iv)

    otokens = discover_active_otokens()
    if not otokens:
        logger.warning("No active oTokens found on-chain, skipping publish")
        return

    matched = match_quotes_to_otokens(quotes, otokens)
    if not matched:
        logger.warning("No quotes matched to oTokens, skipping publish")
        return

    addresses = [m[0] for m in matched]
    bids = [m[1] for m in matched]
    asks = [m[2] for m in matched]
    deadlines = [m[3] for m in matched]
    max_amounts = [settings.default_max_amount_wei] * len(matched)

    ps = get_price_sheet()
    account = get_operator_account()

    tx_fn = ps.functions.publishQuotes(addresses, bids, asks, deadlines, max_amounts)
    tx_hash = build_and_send_tx(tx_fn, account)
    logger.info(f"Published {len(matched)} quotes, tx: {tx_hash}")


async def run():
    """Main loop: publish prices every N seconds."""
    logger.info(f"Price publisher starting (interval={settings.price_publish_interval_seconds}s)")
    while True:
        try:
            await publish_once()
        except Exception:
            logger.exception("Price publish failed")
        await asyncio.sleep(settings.price_publish_interval_seconds)
