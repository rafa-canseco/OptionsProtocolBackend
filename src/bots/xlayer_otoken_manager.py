"""XLayer oToken Manager Bot.

Copy of otoken_manager.py targeting XLayer testnet contracts.
Creates oTokens, whitelists them, and records in available_otokens.
"""

import asyncio
import logging
from datetime import datetime, timezone

from web3 import Web3

from src.config import settings
from src.contracts.web3_client import (
    build_and_send_xlayer_tx,
    get_operator_account,
    get_xlayer_otoken_factory,
    get_xlayer_whitelist,
)
from src.db.database import get_client
from src.pricing.assets import Asset, get_asset_config, get_xlayer_assets
from src.pricing.black_scholes import OptionType
from src.pricing.chainlink import get_asset_price
from src.pricing.price_sheet import OTokenSpec, generate_otoken_specs
from src.pricing.utils import cutoff_hours_for_expiry, strike_to_8_decimals

logger = logging.getLogger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _find_or_create_otoken(
    factory,
    account,
    underlying,
    usdc,
    collateral,
    strike_price,
    expiry,
    is_put,
    label,
) -> str | None:
    target_addr = factory.functions.getTargetOTokenAddress(
        underlying, usdc, collateral, strike_price, expiry, is_put
    ).call()

    if factory.functions.isOToken(target_addr).call():
        logger.debug("oToken exists: %s -> %s", label, target_addr)
        return target_addr

    logger.info("Creating oToken on XLayer: %s", label)
    try:
        tx_fn = factory.functions.createOToken(
            underlying,
            usdc,
            collateral,
            strike_price,
            expiry,
            is_put,
        )
        tx_hash = build_and_send_xlayer_tx(tx_fn, account)
        logger.info("oToken created, tx: %s", tx_hash)
    except Exception as create_err:
        if factory.functions.isOToken(target_addr).call():
            logger.info("oToken already existed (race): %s", label)
            return target_addr
        raise RuntimeError(f"Failed to create oToken: {label}") from create_err

    otoken_addr = factory.functions.getTargetOTokenAddress(
        underlying,
        usdc,
        collateral,
        strike_price,
        expiry,
        is_put,
    ).call()

    if otoken_addr == ZERO_ADDRESS:
        logger.error("oToken tx succeeded but address is zero: %s", label)
        return None

    return otoken_addr


def _whitelist_otoken(otoken_addr, account, label):
    if not settings.xlayer_whitelist_address:
        return

    whitelist = get_xlayer_whitelist()
    if whitelist.functions.isWhitelistedOToken(otoken_addr).call():
        return

    try:
        tx_fn = whitelist.functions.whitelistOToken(otoken_addr)
        wl_hash = build_and_send_xlayer_tx(tx_fn, account)
        logger.info("Whitelisted oToken %s, tx: %s", otoken_addr, wl_hash)
    except Exception:
        if whitelist.functions.isWhitelistedOToken(otoken_addr).call():
            return
        raise


def ensure_otokens_exist(
    specs: list[OTokenSpec],
    asset: Asset = Asset.OKB,
) -> list[tuple[str, OTokenSpec]]:
    factory = get_xlayer_otoken_factory()
    account = get_operator_account()
    cfg = get_asset_config(asset)
    underlying = Web3.to_checksum_address(cfg.underlying_address)
    usdc = Web3.to_checksum_address(settings.xlayer_usdc_address)

    seen: dict[tuple, str | None] = {}
    results: list[tuple[str, OTokenSpec]] = []

    for spec in specs:
        is_put = spec.option_type == OptionType.PUT
        key = (spec.strike, spec.expiry_ts, is_put)
        label = (
            f"strike={spec.strike} "
            f"expiry={datetime.fromtimestamp(spec.expiry_ts, tz=timezone.utc):%Y-%m-%d} "
            f"{'put' if is_put else 'call'}"
        )

        if key in seen:
            if seen[key] is not None:
                results.append((seen[key], spec))
            continue

        strike_price = strike_to_8_decimals(spec.strike)
        collateral = usdc if is_put else underlying

        try:
            otoken_addr = _find_or_create_otoken(
                factory,
                account,
                underlying,
                usdc,
                collateral,
                strike_price,
                spec.expiry_ts,
                is_put,
                label,
            )
        except Exception:
            logger.exception("Failed oToken lookup/create: %s", label)
            seen[key] = None
            continue

        if otoken_addr is None:
            seen[key] = None
            continue

        try:
            _whitelist_otoken(otoken_addr, account, label)
        except Exception:
            logger.exception("Failed to whitelist %s", otoken_addr)
            seen[key] = None
            continue

        seen[key] = otoken_addr
        results.append((otoken_addr, spec))

    return results


def _prune_near_expiry_otokens():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    max_cutoff_ts = now_ts + settings.expiry_cutoff_hours * 3600
    client = get_client()
    result = (
        client.table("available_otokens")
        .select("id, expiry")
        .eq("chain", "xlayer")
        .lt("expiry", max_cutoff_ts)
        .execute()
    )
    rows = result.data or []
    prune_ids = [
        r["id"]
        for r in rows
        if r.get("expiry") is not None
        and r["expiry"] <= now_ts + cutoff_hours_for_expiry(r["expiry"], now_ts) * 3600
    ]
    if prune_ids:
        client.table("available_otokens").delete().in_("id", prune_ids).execute()
    logger.info("Pruned %d available_otokens (xlayer)", len(prune_ids))


def _upsert_available_otokens(paired, asset=Asset.OKB):
    cfg = get_asset_config(asset)
    underlying = cfg.underlying_address.lower()

    seen_addresses: set[str] = set()
    rows = []
    for otoken_addr, spec in paired:
        addr_lower = otoken_addr.lower()
        if addr_lower in seen_addresses:
            continue
        seen_addresses.add(addr_lower)

        is_put = spec.option_type == OptionType.PUT
        usdc = settings.xlayer_usdc_address.lower()
        collateral = usdc if is_put else underlying

        rows.append(
            {
                "otoken_address": addr_lower,
                "underlying": underlying,
                "strike_price": spec.strike,
                "expiry": spec.expiry_ts,
                "is_put": is_put,
                "collateral_asset": collateral,
                "chain": "xlayer",
            }
        )

    if not rows:
        return

    client = get_client()
    client.table("available_otokens").upsert(
        rows, on_conflict="otoken_address"
    ).execute()
    logger.info("Upserted %d oTokens to available_otokens (xlayer)", len(rows))


def _parse_custom_expiries() -> list[int] | None:
    raw = settings.custom_expiry_timestamps.strip()
    if not raw:
        return None
    try:
        timestamps = [int(t.strip()) for t in raw.split(",") if t.strip()]
    except ValueError:
        logger.error("CUSTOM_EXPIRY_TIMESTAMPS malformed: %r", raw)
        return None
    return timestamps or None


async def publish_once():
    _prune_near_expiry_otokens()
    custom_expiries = _parse_custom_expiries()

    for asset in get_xlayer_assets():
        try:
            spot, _ = get_asset_price(asset)
        except Exception:
            logger.exception("Failed to fetch %s price, skipping", asset.value)
            continue

        specs = generate_otoken_specs(
            spot=spot, asset=asset, expiry_timestamps=custom_expiries
        )
        paired = await asyncio.to_thread(ensure_otokens_exist, specs, asset)
        if not paired:
            logger.warning("No oTokens created for %s", asset.value)
            continue

        _upsert_available_otokens(paired, asset)
        logger.info(
            "XLayer oToken manager cycle for %s: %d oTokens",
            asset.value,
            len(paired),
        )


async def run():
    if not settings.xlayer_otoken_factory_address:
        logger.error("xlayer_otoken_factory_address not configured")
        return
    if not settings.operator_private_key:
        logger.error("operator_private_key not configured")
        return

    logger.info(
        "XLayer oToken manager starting (interval=%ds)",
        settings.otoken_publish_interval_seconds,
    )
    while True:
        try:
            await publish_once()
        except Exception:
            logger.exception("XLayer oToken manager cycle failed")
        await asyncio.sleep(settings.otoken_publish_interval_seconds)
