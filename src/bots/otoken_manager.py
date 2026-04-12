"""oToken Manager Bot.

Creates oTokens on-chain via OTokenFactory, whitelists them,
and records them in the available_otokens table so that
external MMs can discover them via GET /mm/market.

Does NOT sign quotes or write to mm_quotes. That is the MM's job.
"""

import asyncio
import logging
from datetime import datetime, timezone

from web3 import Web3

from src.config import settings
from src.contracts.web3_client import (
    build_and_send_tx,
    get_operator_account,
    get_otoken_factory,
    get_whitelist,
)
from src.db.database import get_client
from src.pricing.assets import Asset, get_asset_config, get_base_assets
from src.pricing.black_scholes import OptionType
from src.pricing.price_sheet import OTokenSpec, generate_otoken_specs
from src.pricing.utils import strike_to_8_decimals
from src.pricing.chainlink import get_asset_price

logger = logging.getLogger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _find_or_create_otoken(
    factory,
    account,
    underlying: str,
    usdc: str,
    collateral: str,
    strike_price: int,
    expiry: int,
    is_put: bool,
    label: str,
) -> str | None:
    """Find an existing oToken or create a new one.

    Returns the oToken address, or None if creation failed.
    """
    target_addr = factory.functions.getTargetOTokenAddress(
        underlying, usdc, collateral, strike_price, expiry, is_put
    ).call()

    if factory.functions.isOToken(target_addr).call():
        logger.debug("oToken exists: %s -> %s", label, target_addr)
        return target_addr

    logger.info("Creating oToken: %s", label)
    try:
        tx_fn = factory.functions.createOToken(
            underlying,
            usdc,
            collateral,
            strike_price,
            expiry,
            is_put,
        )
        tx_hash = build_and_send_tx(tx_fn, account)
        logger.info("oToken created, tx: %s", tx_hash)
    except Exception as create_err:
        # Race condition: another process may have created it
        if factory.functions.isOToken(target_addr).call():
            logger.info(
                "oToken already existed (race): %s -> %s",
                label,
                target_addr,
            )
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
        logger.error(
            "oToken tx succeeded (%s) but address is zero: %s",
            tx_hash,
            label,
        )
        return None

    logger.info("oToken address resolved: %s -> %s", label, otoken_addr)
    return otoken_addr


def _whitelist_otoken(otoken_addr: str, account, label: str) -> None:
    """Ensure an oToken is whitelisted. Raises on failure."""
    if not settings.whitelist_address:
        return

    whitelist = get_whitelist()
    if whitelist.functions.isWhitelistedOToken(otoken_addr).call():
        return

    try:
        tx_fn = whitelist.functions.whitelistOToken(otoken_addr)
        wl_hash = build_and_send_tx(tx_fn, account)
        logger.info("Whitelisted oToken %s, tx: %s", otoken_addr, wl_hash)
    except Exception:
        if whitelist.functions.isWhitelistedOToken(otoken_addr).call():
            logger.info(
                "oToken %s already whitelisted (by factory), skipping: %s",
                otoken_addr,
                label,
            )
            return
        raise


def ensure_otokens_exist(
    specs: list[OTokenSpec],
    asset: Asset = Asset.ETH,
) -> list[tuple[str, OTokenSpec]]:
    """For each spec, ensure the corresponding oToken exists on-chain.

    Deduplicates by (strike, expiry_ts, is_put) to avoid redundant
    on-chain calls. Skips individual specs on failure without aborting
    the whole cycle. Returns (otoken_address, spec) pairs.
    """
    factory = get_otoken_factory()
    account = get_operator_account()
    cfg = get_asset_config(asset)
    underlying = Web3.to_checksum_address(cfg.underlying_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    seen: dict[tuple, str | None] = {}
    results: list[tuple[str, OTokenSpec]] = []

    for spec in specs:
        is_put = spec.option_type == OptionType.PUT
        key = (spec.strike, spec.expiry_ts, is_put)
        expiry_date = datetime.fromtimestamp(spec.expiry_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        label = (
            f"strike={spec.strike} expiry={expiry_date} {'put' if is_put else 'call'}"
        )

        if key in seen:
            if seen[key] is not None:
                results.append((seen[key], spec))
            continue

        strike_price = strike_to_8_decimals(spec.strike)
        expiry = spec.expiry_ts
        collateral = usdc if is_put else underlying

        try:
            otoken_addr = _find_or_create_otoken(
                factory,
                account,
                underlying,
                usdc,
                collateral,
                strike_price,
                expiry,
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
            logger.exception(
                "Failed to whitelist %s: %s. Excluding.",
                otoken_addr,
                label,
            )
            seen[key] = None
            continue

        seen[key] = otoken_addr
        results.append((otoken_addr, spec))

    return results


def _is_valid_expiry(ts: int) -> bool:
    """Return True if timestamp is a valid expiry (08:00 UTC on any day)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.hour == 8 and dt.minute == 0 and dt.second == 0


def _prune_near_expiry_otokens() -> None:
    """Delete rows from available_otokens within their dynamic cutoff.

    Short-term expiries (TTL <= 48h) use short cutoff (4h).
    Standard expiries use standard cutoff (48h).
    """
    from src.pricing.utils import cutoff_hours_for_expiry

    now_ts = int(datetime.now(timezone.utc).timestamp())
    max_cutoff_ts = now_ts + settings.expiry_cutoff_hours * 3600
    client = get_client()
    result = (
        client.table("available_otokens")
        .select("id, expiry")
        .eq("chain", "base")
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
    logger.info("Pruned %d available_otokens", len(prune_ids))


def _upsert_available_otokens(
    paired: list[tuple[str, OTokenSpec]],
    asset: Asset = Asset.ETH,
) -> None:
    """Write created oTokens to the available_otokens table.

    Skips any spec whose expiry is not 08:00 UTC.
    Raises on DB failure so the caller knows the cycle did not
    complete successfully.
    """
    cfg = get_asset_config(asset)
    underlying = cfg.underlying_address.lower()

    seen_addresses: set[str] = set()
    rows = []
    for otoken_addr, spec in paired:
        if not _is_valid_expiry(spec.expiry_ts):
            expiry_dt = datetime.fromtimestamp(spec.expiry_ts, tz=timezone.utc)
            logger.warning(
                "Skipping invalid expiry oToken: %s expiry=%s",
                otoken_addr,
                expiry_dt.isoformat(),
            )
            continue

        addr_lower = otoken_addr.lower()
        if addr_lower in seen_addresses:
            continue
        seen_addresses.add(addr_lower)

        is_put = spec.option_type == OptionType.PUT
        usdc = settings.usdc_address.lower()
        collateral = usdc if is_put else underlying

        rows.append(
            {
                "otoken_address": addr_lower,
                "underlying": underlying,
                "strike_price": spec.strike,
                "expiry": spec.expiry_ts,
                "is_put": is_put,
                "collateral_asset": collateral,
                "chain": "base",
            }
        )

    if not rows:
        return

    client = get_client()
    client.table("available_otokens").upsert(
        rows, on_conflict="otoken_address"
    ).execute()
    logger.info("Upserted %d oTokens to available_otokens", len(rows))


def _parse_custom_expiries() -> list[int] | None:
    """Parse CUSTOM_EXPIRY_TIMESTAMPS env var into a list of ints, or None if unset."""
    raw = settings.custom_expiry_timestamps.strip()
    if not raw:
        return None
    try:
        timestamps = [int(t.strip()) for t in raw.split(",") if t.strip()]
    except ValueError:
        logger.error(
            "CUSTOM_EXPIRY_TIMESTAMPS is malformed: %r — using default expiries",
            raw,
        )
        return None
    if not timestamps:
        return None
    logger.info("Using custom expiry timestamps: %s", timestamps)
    return timestamps


async def publish_once():
    """Single cycle: prune stale oTokens, generate specs for each asset, create on-chain."""
    _prune_near_expiry_otokens()

    custom_expiries = _parse_custom_expiries()

    for asset in get_base_assets():
        try:
            spot, _ = get_asset_price(asset)
        except Exception:
            logger.exception("Failed to fetch %s price, skipping asset", asset.value)
            continue

        specs = generate_otoken_specs(
            spot=spot, asset=asset, expiry_timestamps=custom_expiries
        )

        paired = await asyncio.to_thread(ensure_otokens_exist, specs, asset)
        if not paired:
            logger.warning("No oTokens created for %s, skipping", asset.value)
            continue

        _upsert_available_otokens(paired, asset)
        logger.info(
            "oToken manager cycle for %s complete: %d oTokens",
            asset.value,
            len(paired),
        )


async def run():
    """Main loop: ensure oTokens exist every N seconds."""
    if not settings.otoken_factory_address:
        logger.error(
            "otoken_factory_address not configured, otoken manager cannot start"
        )
        return
    if not settings.operator_private_key:
        logger.error("operator_private_key not configured, otoken manager cannot start")
        return

    logger.info(
        "oToken manager starting (interval=%ds)",
        settings.otoken_publish_interval_seconds,
    )
    while True:
        try:
            await publish_once()
        except Exception:
            logger.exception("oToken manager cycle failed")
        await asyncio.sleep(settings.otoken_publish_interval_seconds)
