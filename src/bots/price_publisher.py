"""
Price Publisher Bot

Generates Black-Scholes prices, ensures oTokens exist on-chain
for each quote, then publishes quotes to PriceSheet.
"""
import asyncio
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
    get_whitelist,
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
    Anchored to the next 08:00 UTC boundary so that all calls within
    the same 24h window (08:00→08:00) produce the same timestamp.
    If days=0 and current time is past 08:00 UTC, returns tomorrow's 08:00.
    """
    now = datetime.now(timezone.utc)
    today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if today_8am <= now:
        base = today_8am + timedelta(days=1)
    else:
        base = today_8am
    expiry_dt = base + timedelta(days=days)
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

    Deduplicates by (strike, expiry_days, is_put) to avoid redundant on-chain calls.
    Creates oTokens via OTokenFactory.createOToken if they don't exist yet.
    Handles OTokenAlreadyExists gracefully (reads existing address).
    Skips individual quotes on failure without aborting the whole cycle.
    Returns a list of (otoken_address, quote) pairs.
    """
    factory = get_otoken_factory()
    account = get_operator_account()
    weth = Web3.to_checksum_address(settings.weth_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    # (strike, expiry_days, is_put) → otoken address (None = failed)
    seen: dict[tuple, str | None] = {}
    results: list[tuple[str, PriceQuote]] = []

    for quote in quotes:
        is_put = quote.option_type == OptionType.PUT
        key = (quote.strike, quote.expiry_days, is_put)
        label = f"strike={quote.strike} expiry={quote.expiry_days}d {'put' if is_put else 'call'}"

        if key in seen:
            if seen[key] is not None:
                results.append((seen[key], quote))
            continue

        strike_price = strike_to_8_decimals(quote.strike)
        expiry = expiry_days_to_timestamp(quote.expiry_days)
        collateral = usdc if is_put else weth

        # Step 1: check if oToken already exists
        try:
            params_hash = compute_params_hash(weth, usdc, collateral, strike_price, expiry, is_put)
            existing = factory.functions.getOToken(params_hash).call()
        except Exception:
            logger.exception(f"Failed to check oToken existence: {label}")
            seen[key] = None
            continue

        if existing != ZERO_ADDRESS:
            otoken_addr = existing
            logger.debug(f"oToken exists: {label} → {otoken_addr}")
        else:
            # Step 2: create the oToken
            try:
                logger.info(f"Creating oToken: {label}")
                tx_fn = factory.functions.createOToken(weth, usdc, collateral, strike_price, expiry, is_put)
                tx_hash = build_and_send_tx(tx_fn, account)
                logger.info(f"oToken created, tx: {tx_hash}")
            except Exception:
                # Handle OTokenAlreadyExists (race condition: another actor created it)
                try:
                    addr = factory.functions.getOToken(params_hash).call()
                    if addr != ZERO_ADDRESS:
                        logger.info(f"oToken already existed (race condition): {label} → {addr}")
                        otoken_addr = addr
                    else:
                        logger.exception(f"Failed to create oToken: {label}")
                        seen[key] = None
                        continue
                except Exception:
                    logger.debug(f"Recovery getOToken also failed for {label}", exc_info=True)
                    logger.exception(f"Failed to create oToken: {label}")
                    seen[key] = None
                    continue
            else:
                # Step 3: read back the newly created address
                try:
                    otoken_addr = factory.functions.getOToken(params_hash).call()
                except Exception:
                    logger.exception(f"oToken created (tx: {tx_hash}) but failed to read address: {label}")
                    seen[key] = None
                    continue

                if otoken_addr == ZERO_ADDRESS:
                    logger.error(f"oToken creation tx succeeded ({tx_hash}) but getOToken returned zero: {label}")
                    seen[key] = None
                    continue

        # Ensure oToken is whitelisted (runs for both new and existing oTokens)
        if settings.whitelist_address:
            try:
                whitelist = get_whitelist()
                is_wl = whitelist.functions.isWhitelistedOToken(otoken_addr).call()
                if not is_wl:
                    tx_fn = whitelist.functions.whitelistOToken(otoken_addr)
                    wl_hash = build_and_send_tx(tx_fn, account)
                    logger.info(f"Whitelisted oToken {otoken_addr}, tx: {wl_hash}")
            except Exception:
                logger.exception(f"Failed to whitelist oToken {otoken_addr}: {label}")

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
    if not settings.otoken_factory_address:
        logger.error("otoken_factory_address not configured, price publisher cannot start")
        return
    if not settings.operator_private_key:
        logger.error("operator_private_key not configured, price publisher cannot start")
        return

    logger.info(f"Price publisher starting (interval={settings.price_publish_interval_seconds}s)")
    while True:
        try:
            await publish_once()
        except Exception:
            logger.exception("Price publish failed")
        await asyncio.sleep(settings.price_publish_interval_seconds)
