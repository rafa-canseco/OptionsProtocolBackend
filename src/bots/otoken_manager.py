"""oToken Manager Bot.

Publishes deterministic oToken addresses for MMs and, outside lazy cohorts,
creates/whitelists them eagerly for rollback compatibility.

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
from src.otokens.models import CanonicalSeries
from src.otokens.repository import SeriesRepository

logger = logging.getLogger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
OTokenKey = tuple[float, int, bool]
# Run a full on-chain reconcile every Nth publish cycle. With the default
# 5-min cadence this bounds the stale-whitelist window to ~15 min: an
# externally-removed oToken whitelist is re-detected and re-whitelisted
# within 2 DB-fast cycles at most before the market surface shows it
# as unusable to MMs.
FULL_RECONCILE_EVERY_CYCLES = 3
_publish_cycle_count = 0
VALID_SERIES_MODES = {"eager", "shadow", "lazy"}


def get_otoken_series_mode() -> str:
    mode = settings.otoken_series_mode.strip().lower()
    if mode not in VALID_SERIES_MODES:
        raise RuntimeError("OTOKEN_SERIES_MODE must be one of eager, shadow, lazy")
    return mode


def _lazy_assets() -> set[str]:
    return {
        item.strip().lower()
        for item in settings.otoken_lazy_assets.split(",")
        if item.strip()
    }


def _spec_key(spec: OTokenSpec) -> OTokenKey:
    return (spec.strike, spec.expiry_ts, spec.option_type == OptionType.PUT)


def _canonical_series(spec: OTokenSpec, asset: Asset) -> CanonicalSeries:
    cfg = get_asset_config(asset)
    is_put = spec.option_type == OptionType.PUT
    underlying = Web3.to_checksum_address(cfg.underlying_address)
    strike_asset = Web3.to_checksum_address(settings.usdc_address)
    collateral = strike_asset if is_put else underlying
    return CanonicalSeries(
        chain_id=settings.chain_id,
        factory_address=settings.otoken_factory_address,
        underlying=underlying,
        strike_asset=strike_asset,
        collateral_asset=collateral,
        strike_price_raw=strike_to_8_decimals(spec.strike),
        expiry=spec.expiry_ts,
        is_put=is_put,
    )


def _publish_virtual_otokens(
    specs: list[OTokenSpec],
    asset: Asset,
) -> int:
    """Insert unseen virtual specs with their exact future CREATE2 address."""
    if not specs:
        return 0
    repository = SeriesRepository()
    canonical_by_key = {
        canonical.series_key: (canonical, spec)
        for spec in specs
        for canonical in (_canonical_series(spec, asset),)
    }
    cfg = get_asset_config(asset)
    result = (
        repository.client.table("available_otokens")
        .select(
            "id,series_key,otoken_address,strike_price,expiry,is_put,deployment_status"
        )
        .eq("chain", "base")
        .eq("underlying", cfg.underlying_address.lower())
        .in_("expiry", sorted({spec.expiry_ts for spec in specs}))
        .execute()
    )
    existing_rows = result.data or []
    existing = {
        str(row["series_key"]) for row in existing_rows if row.get("series_key")
    }
    legacy_by_spec = {
        (
            float(row["strike_price"]),
            int(row["expiry"]),
            bool(row["is_put"]),
        ): row
        for row in existing_rows
        if not row.get("series_key")
    }

    factory = get_otoken_factory()
    rows = []
    for series_key, (canonical, spec) in canonical_by_key.items():
        if series_key in existing:
            continue
        predicted = factory.functions.getTargetOTokenAddress(
            *canonical.factory_args
        ).call()
        if predicted.lower() == ZERO_ADDRESS:
            raise RuntimeError("Factory returned zero CREATE2 target")
        legacy = legacy_by_spec.get(_spec_key(spec))
        if legacy and str(legacy["otoken_address"]).lower() == predicted.lower():
            canonical_row = canonical.to_row(
                otoken_address=predicted,
                strike_price=spec.strike,
                deployment_status=str(legacy.get("deployment_status") or "ready"),
            )
            metadata = {
                key: value
                for key, value in canonical_row.items()
                if key not in {"otoken_address", "deployment_status"}
            }
            (
                repository.client.table("available_otokens")
                .update(metadata)
                .eq("id", legacy["id"])
                .is_("series_key", "null")
                .execute()
            )
            continue
        rows.append(
            canonical.to_row(
                otoken_address=predicted,
                strike_price=spec.strike,
                deployment_status="virtual",
            )
        )
    repository.insert_virtual_rows(rows)
    return len(rows)


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
    asset: Asset | dict[OTokenKey, str] = Asset.ETH,
    existing_by_key: dict[OTokenKey, str] | None = None,
) -> list[tuple[str, OTokenSpec]]:
    """For each spec, ensure the corresponding oToken exists on-chain.

    Deduplicates by (strike, expiry_ts, is_put) to avoid redundant
    on-chain calls. Skips individual specs on failure without aborting
    the whole cycle. Returns (otoken_address, spec) pairs.
    """
    if isinstance(asset, dict):
        existing_by_key = asset
        asset = Asset.ETH

    cfg = get_asset_config(asset)
    underlying = Web3.to_checksum_address(cfg.underlying_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    existing_by_key = existing_by_key or {}
    seen: dict[tuple, str | None] = {}
    results: list[tuple[str, OTokenSpec]] = []
    factory = None
    account = None

    for spec in specs:
        is_put = spec.option_type == OptionType.PUT
        key = _spec_key(spec)
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

        existing_addr = existing_by_key.get(key)
        if existing_addr:
            seen[key] = existing_addr
            results.append((existing_addr, spec))
            continue

        if factory is None:
            factory = get_otoken_factory()
        if account is None:
            account = get_operator_account()

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


def _load_existing_otokens_for_specs(
    specs: list[OTokenSpec],
    asset: Asset = Asset.ETH,
) -> dict[OTokenKey, str]:
    """Load already-published oTokens from DB for the target specs.

    This keeps the normal market surface unchanged while avoiding repeated
    on-chain existence and whitelist checks for rows the bot has already
    published to `available_otokens`.
    """
    if not specs:
        return {}

    target_keys = {_spec_key(spec) for spec in specs}
    now_ts = int(datetime.now(timezone.utc).timestamp())
    underlying = get_asset_config(asset).underlying_address.lower()
    client = get_client()
    result = (
        client.table("available_otokens")
        .select(
            "otoken_address,strike_price,expiry,is_put,underlying,deployment_status"
        )
        .gt("expiry", now_ts)
        .execute()
    )
    if result.data is None:
        raise RuntimeError("available_otokens query returned data=None")

    existing: dict[OTokenKey, str] = {}
    for row in result.data:
        try:
            if row.get("deployment_status", "ready") != "ready":
                continue
            row_underlying = row.get("underlying")
            if row_underlying is not None and str(row_underlying).lower() != underlying:
                continue
            key = (
                float(row["strike_price"]),
                int(row["expiry"]),
                bool(row["is_put"]),
            )
            addr = str(row["otoken_address"]).lower()
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed available_otokens row: %s", row)
            continue
        if key in target_keys and key not in existing:
            existing[key] = addr

    return existing


def _is_valid_expiry(ts: int) -> bool:
    """Return True if timestamp is a valid expiry (08:00 UTC on any day)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.hour == 8 and dt.minute == 0 and dt.second == 0


def _prune_near_expiry_otokens() -> None:
    """Delete unmaterialized rows within their dynamic cutoff.

    Ready rows are retained for historical creation-to-fill telemetry and
    existing position metadata. Public/MM queries already filter active expiries.
    """
    from src.pricing.utils import cutoff_hours_for_expiry

    now_ts = int(datetime.now(timezone.utc).timestamp())
    max_cutoff_ts = now_ts + settings.expiry_cutoff_hours * 3600
    client = get_client()
    result = (
        client.table("available_otokens")
        .select("id, expiry, deployment_status")
        .eq("chain", "base")
        .lt("expiry", max_cutoff_ts)
        .execute()
    )
    rows = result.data or []
    prune_ids = [
        r["id"]
        for r in rows
        if r.get("expiry") is not None
        and r.get("deployment_status") != "ready"
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

        cfg = get_asset_config(asset)
        is_put = spec.option_type == OptionType.PUT
        collateral = settings.usdc_address if is_put else cfg.underlying_address
        row = {
            "otoken_address": addr_lower,
            "underlying": cfg.underlying_address.lower(),
            "collateral_asset": collateral.lower(),
            "strike_price": spec.strike,
            "expiry": spec.expiry_ts,
            "is_put": is_put,
            "chain": "base",
            "deployment_status": "ready",
            "ready_at": datetime.now(timezone.utc).isoformat(),
        }
        # Preserve eager rollback compatibility in minimally configured tooling.
        # Runtime environments always configure the factory and receive complete
        # canonical lifecycle metadata.
        if settings.otoken_factory_address:
            canonical = _canonical_series(spec, asset)
            row |= canonical.to_row(
                otoken_address=addr_lower,
                strike_price=spec.strike,
                deployment_status="ready",
            )
        rows.append(row)

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
    """Publish active specs and create only according to the rollout mode."""
    global _publish_cycle_count
    _publish_cycle_count += 1

    _prune_near_expiry_otokens()

    custom_expiries = _parse_custom_expiries()
    mode = get_otoken_series_mode()
    lazy_assets = _lazy_assets()

    for asset in get_base_assets():
        try:
            spot, _ = get_asset_price(asset)
        except Exception:
            logger.exception("Failed to fetch %s price, skipping asset", asset.value)
            continue

        specs = generate_otoken_specs(
            spot=spot, asset=asset, expiry_timestamps=custom_expiries
        )

        if mode == "lazy" and asset.value in lazy_assets:
            inserted = await asyncio.to_thread(
                _publish_virtual_otokens,
                specs,
                asset,
            )
            logger.info(
                "oToken manager lazy cycle for %s: target=%d newly_virtual=%d",
                asset.value,
                len({_spec_key(spec) for spec in specs}),
                inserted,
            )
            continue

        if _publish_cycle_count % FULL_RECONCILE_EVERY_CYCLES == 1:
            existing_by_key = {}
            logger.info(
                "oToken manager full reconciliation cycle for %s: "
                "checking target specs on-chain",
                asset.value,
            )
        else:
            existing_by_key = await asyncio.to_thread(
                _load_existing_otokens_for_specs,
                specs,
                asset,
            )
            logger.info(
                "oToken manager DB diff for %s: %d/%d target specs already published",
                asset.value,
                len(existing_by_key),
                len({_spec_key(spec) for spec in specs}),
            )

        paired = await asyncio.to_thread(
            ensure_otokens_exist,
            specs,
            asset,
            existing_by_key,
        )
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
