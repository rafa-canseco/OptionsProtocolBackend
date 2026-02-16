"""
Price Publisher Bot

Generates Black-Scholes prices, ensures oTokens exist on-chain
for each quote, then publishes quotes to PriceSheet.
"""
import asyncio
import calendar
import logging
import time
from datetime import datetime, timezone, timedelta

from web3 import Web3

from src.config import settings
from src.pricing.chainlink import get_eth_price
from src.pricing.deribit import get_eth_iv
from src.pricing.price_sheet import generate_price_sheet, PriceQuote
from src.pricing.black_scholes import OptionType
from src.contracts.web3_client import (
    get_price_sheet,
    get_otoken_factory,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)

SPREAD = 0.01  # 1% bid-ask spread
USDC_DECIMALS = 6
STRIKE_DECIMALS = 8
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def premium_to_usdc(premium_usd: float) -> int:
    """Convert a USD premium float to USDC 6-decimal integer."""
    return max(int(premium_usd * 10**USDC_DECIMALS), 1)


def strike_to_8_decimals(strike_usd: float) -> int:
    """Convert a strike price in USD to 8-decimal integer (e.g. $2000 → 200000000000)."""
    return int(strike_usd * 10**STRIKE_DECIMALS)


def expiry_days_to_timestamp(days: int) -> int:
    """Convert days-from-now to a UTC 08:00 expiry timestamp.

    The contract requires expiry % 86400 == 28800 (08:00 UTC).
    We snap to 08:00 UTC of the target day.
    """
    now = datetime.now(timezone.utc)
    target = now + timedelta(days=days)
    # Snap to 08:00 UTC of the target day
    expiry_dt = target.replace(hour=8, minute=0, second=0, microsecond=0)
    # If we've already passed 08:00 today and days=0, push to next day
    if expiry_dt <= now:
        expiry_dt += timedelta(days=1)
    return int(expiry_dt.timestamp())


def compute_params_hash(
    underlying: str,
    strike_asset: str,
    collateral_asset: str,
    strike_price: int,
    expiry: int,
    is_put: bool,
) -> bytes:
    """Reproduce Solidity's keccak256(abi.encodePacked(...)) in Python."""
    return Web3.solidity_keccak(
        ["address", "address", "address", "uint256", "uint256", "bool"],
        [
            Web3.to_checksum_address(underlying),
            Web3.to_checksum_address(strike_asset),
            Web3.to_checksum_address(collateral_asset),
            strike_price,
            expiry,
            is_put,
        ],
    )


def ensure_otokens_exist(quotes: list[PriceQuote]) -> list[tuple[str, PriceQuote]]:
    """For each quote, ensure the corresponding oToken exists on-chain.

    Creates oTokens via OTokenFactory.createOToken if they don't exist yet.
    Returns a list of (otoken_address, quote) pairs.
    """
    factory = get_otoken_factory()
    account = get_operator_account()
    weth = Web3.to_checksum_address(settings.weth_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    # Deduplicate by (strike, expiry_days, option_type) — each combo maps to one oToken
    seen: dict[tuple, str] = {}  # (strike, expiry_days, is_put) → otoken address
    results: list[tuple[str, PriceQuote]] = []

    for quote in quotes:
        is_put = quote.option_type == OptionType.PUT
        key = (quote.strike, quote.expiry_days, is_put)

        if key in seen:
            results.append((seen[key], quote))
            continue

        strike_price = strike_to_8_decimals(quote.strike)
        expiry = expiry_days_to_timestamp(quote.expiry_days)
        collateral = usdc if is_put else weth

        params_hash = compute_params_hash(weth, usdc, collateral, strike_price, expiry, is_put)
        existing = factory.functions.getOToken(params_hash).call()

        if existing != ZERO_ADDRESS:
            logger.debug(f"oToken exists: strike={quote.strike} expiry={quote.expiry_days}d {'put' if is_put else 'call'} → {existing}")
            seen[key] = existing
            results.append((existing, quote))
            continue

        # Create the oToken
        logger.info(f"Creating oToken: strike={quote.strike} expiry={quote.expiry_days}d {'put' if is_put else 'call'}")
        tx_fn = factory.functions.createOToken(weth, usdc, collateral, strike_price, expiry, is_put)
        tx_hash = build_and_send_tx(tx_fn, account)
        logger.info(f"oToken created, tx: {tx_hash}")

        # Read the address of the newly created oToken
        otoken_addr = factory.functions.getOToken(params_hash).call()
        seen[key] = otoken_addr
        results.append((otoken_addr, quote))

    return results


async def publish_once():
    """Single publish cycle: generate prices, ensure oTokens, publish on-chain."""
    eth_price, _ = get_eth_price()
    iv = await get_eth_iv()
    quotes = generate_price_sheet(spot=eth_price, iv=iv)

    paired = ensure_otokens_exist(quotes)
    if not paired:
        logger.warning("No quotes to publish, skipping")
        return

    now = int(time.time())
    addresses = []
    bids = []
    asks = []
    deadlines = []
    max_amounts = []

    for otoken_addr, quote in paired:
        addresses.append(otoken_addr)
        bids.append(premium_to_usdc(quote.premium * (1 - SPREAD)))
        asks.append(premium_to_usdc(quote.premium * (1 + SPREAD)))
        deadlines.append(now + settings.quote_deadline_seconds)
        max_amounts.append(settings.default_max_amount_wei)

    ps = get_price_sheet()
    account = get_operator_account()

    tx_fn = ps.functions.publishQuotes(addresses, bids, asks, deadlines, max_amounts)
    tx_hash = build_and_send_tx(tx_fn, account)
    logger.info(f"Published {len(paired)} quotes, tx: {tx_hash}")


async def run():
    """Main loop: publish prices every N seconds."""
    logger.info(f"Price publisher starting (interval={settings.price_publish_interval_seconds}s)")
    while True:
        try:
            await publish_once()
        except Exception:
            logger.exception("Price publish failed")
        await asyncio.sleep(settings.price_publish_interval_seconds)
