"""oToken Manager Bot.

Creates oTokens on-chain via OTokenFactory, whitelists them,
and records them in the available_otokens table so that
external MMs can discover them via GET /mm/market.

Does NOT sign quotes or write to mm_quotes. That is the MM's job.
"""
import asyncio
import logging

from web3 import Web3

from src.config import settings
from src.db.database import get_client
from src.pricing.chainlink import get_eth_price
from src.pricing.deribit import get_eth_iv
from src.pricing.price_sheet import generate_price_sheet, PriceQuote
from src.pricing.black_scholes import OptionType
from src.pricing.utils import strike_to_8_decimals, expiry_days_to_timestamp
from src.contracts.web3_client import (
    get_otoken_factory,
    get_whitelist,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)

OTOKEN_DECIMALS = 8
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def ensure_otokens_exist(
    quotes: list[PriceQuote],
) -> list[tuple[str, PriceQuote]]:
    """For each quote, ensure the corresponding oToken exists on-chain.

    Deduplicates by (strike, expiry_days, is_put) to avoid redundant
    on-chain calls. Creates oTokens via OTokenFactory.createOToken if
    they don't exist yet. Handles OTokenAlreadyExists gracefully.
    Skips individual quotes on failure without aborting the whole cycle.
    Returns a list of (otoken_address, quote) pairs.
    """
    factory = get_otoken_factory()
    account = get_operator_account()
    weth = Web3.to_checksum_address(settings.weth_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    seen: dict[tuple, str | None] = {}
    results: list[tuple[str, PriceQuote]] = []

    for quote in quotes:
        is_put = quote.option_type == OptionType.PUT
        key = (quote.strike, quote.expiry_days, is_put)
        label = (
            f"strike={quote.strike} expiry={quote.expiry_days}d "
            f"{'put' if is_put else 'call'}"
        )

        if key in seen:
            if seen[key] is not None:
                results.append((seen[key], quote))
            continue

        strike_price = strike_to_8_decimals(quote.strike)
        expiry = expiry_days_to_timestamp(quote.expiry_days)
        collateral = usdc if is_put else weth

        try:
            target_addr = factory.functions.getTargetOTokenAddress(
                weth, usdc, collateral, strike_price, expiry, is_put
            ).call()
            exists = factory.functions.isOToken(target_addr).call()
        except Exception:
            logger.exception(
                "Failed to check oToken existence: %s", label
            )
            seen[key] = None
            continue

        if exists:
            otoken_addr = target_addr
            logger.debug("oToken exists: %s -> %s", label, otoken_addr)
        else:
            try:
                logger.info("Creating oToken: %s", label)
                tx_fn = factory.functions.createOToken(
                    weth, usdc, collateral,
                    strike_price, expiry, is_put,
                )
                tx_hash = build_and_send_tx(tx_fn, account)
                logger.info("oToken created, tx: %s", tx_hash)
            except Exception:
                try:
                    is_created = factory.functions.isOToken(
                        target_addr
                    ).call()
                    if is_created:
                        logger.info(
                            "oToken already existed (race): %s -> %s",
                            label, target_addr,
                        )
                        otoken_addr = target_addr
                    else:
                        logger.exception(
                            "Failed to create oToken: %s", label
                        )
                        seen[key] = None
                        continue
                except Exception:
                    logger.debug(
                        "Recovery isOToken check failed for %s",
                        label, exc_info=True,
                    )
                    logger.exception(
                        "Failed to create oToken: %s", label
                    )
                    seen[key] = None
                    continue
            else:
                try:
                    otoken_addr = (
                        factory.functions.getTargetOTokenAddress(
                            weth, usdc, collateral,
                            strike_price, expiry, is_put,
                        ).call()
                    )
                except Exception:
                    logger.exception(
                        "oToken created (tx: %s) but failed to "
                        "compute address: %s", tx_hash, label,
                    )
                    seen[key] = None
                    continue

                if otoken_addr == ZERO_ADDRESS:
                    logger.error(
                        "oToken tx succeeded (%s) but "
                        "getTargetOTokenAddress returned zero: %s",
                        tx_hash, label,
                    )
                    seen[key] = None
                    continue

                logger.info(
                    "oToken address resolved: %s -> %s",
                    label, otoken_addr,
                )

        if settings.whitelist_address:
            try:
                whitelist = get_whitelist()
                is_wl = whitelist.functions.isWhitelistedOToken(
                    otoken_addr
                ).call()
                if not is_wl:
                    tx_fn = whitelist.functions.whitelistOToken(
                        otoken_addr
                    )
                    wl_hash = build_and_send_tx(tx_fn, account)
                    logger.info(
                        "Whitelisted oToken %s, tx: %s",
                        otoken_addr, wl_hash,
                    )
            except Exception:
                logger.exception(
                    "Failed to whitelist oToken %s: %s. "
                    "Excluding to prevent user tx reverts.",
                    otoken_addr, label,
                )
                seen[key] = None
                continue

        seen[key] = otoken_addr
        results.append((otoken_addr, quote))

    return results


def _upsert_available_otokens(
    paired: list[tuple[str, PriceQuote]],
) -> None:
    """Write created oTokens to the available_otokens table."""
    seen_addresses: set[str] = set()
    rows = []
    for otoken_addr, quote in paired:
        addr_lower = otoken_addr.lower()
        if addr_lower in seen_addresses:
            continue
        seen_addresses.add(addr_lower)

        is_put = quote.option_type == OptionType.PUT
        weth = settings.weth_address.lower()
        usdc = settings.usdc_address.lower()
        collateral = usdc if is_put else weth

        rows.append({
            "otoken_address": addr_lower,
            "strike_price": quote.strike,
            "expiry": expiry_days_to_timestamp(quote.expiry_days),
            "is_put": is_put,
            "collateral_asset": collateral,
        })

    if not rows:
        return

    try:
        client = get_client()
        client.table("available_otokens").upsert(
            rows, on_conflict="otoken_address"
        ).execute()
        logger.info(
            "Upserted %d oTokens to available_otokens", len(rows)
        )
    except Exception:
        logger.exception("Failed to write available_otokens")


async def publish_once():
    """Single cycle: generate price sheet, create oTokens, record them."""
    eth_price, _ = get_eth_price()
    iv = await get_eth_iv()
    quotes = generate_price_sheet(spot=eth_price, iv=iv)

    paired = await asyncio.to_thread(ensure_otokens_exist, quotes)
    if not paired:
        logger.warning("No oTokens created, skipping")
        return

    _upsert_available_otokens(paired)
    logger.info("oToken manager cycle complete: %d oTokens", len(paired))


async def run():
    """Main loop: ensure oTokens exist every N seconds."""
    if not settings.otoken_factory_address:
        logger.error(
            "otoken_factory_address not configured, "
            "otoken manager cannot start"
        )
        return
    if not settings.operator_private_key:
        logger.error(
            "operator_private_key not configured, "
            "otoken manager cannot start"
        )
        return

    logger.info(
        "oToken manager starting (interval=%ds)",
        settings.price_publish_interval_seconds,
    )
    while True:
        try:
            await publish_once()
        except Exception:
            logger.exception("oToken manager cycle failed")
        await asyncio.sleep(settings.price_publish_interval_seconds)
