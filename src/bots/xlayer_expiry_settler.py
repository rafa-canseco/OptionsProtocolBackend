"""XLayer Expiry Settler Bot.

Copy of expiry_settler.py targeting XLayer testnet contracts.
Always uses beta_mode settlement (oracle price + slippage buffer,
no Uniswap Quoter since XLayer testnet has no live DEX).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from web3 import Web3

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import (
    get_xlayer_batch_settler,
    get_xlayer_controller,
    get_xlayer_oracle,
    get_operator_account,
    build_and_send_xlayer_tx,
)
from src.pricing.assets import Asset, get_asset_config
from src.pricing.chainlink import get_asset_price_raw

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 50
BETA_SLIPPAGE_BPS = 1_000  # 10%
_RETRY_BACKOFF_SECONDS = [60, 300, 900, 1800, 3600]


def get_expired_unsettled() -> list[dict]:
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select(
            "user_address, vault_id, otoken_address, expiry, "
            "amount, strike_price, is_put, mm_address, asset"
        )
        .or_("is_settled.eq.false,is_settled.is.null")
        .eq("chain", "xlayer")
        .lte("expiry", now)
        .not_.is_("strike_price", "null")
        .not_.is_("is_put", "null")
        .not_.is_("amount", "null")
        .execute()
    )
    return result.data or []


def identify_itm_positions(positions):
    oracle = get_xlayer_oracle()
    itm = []
    skipped = set()
    expiry_price_cache = {}

    for pos in positions:
        expiry = pos["expiry"]
        asset_str = pos.get("asset", "okb")
        try:
            cfg = get_asset_config(Asset(asset_str))
        except (ValueError, KeyError):
            cfg = get_asset_config(Asset.OKB)
        underlying = Web3.to_checksum_address(cfg.underlying_address)

        cache_key = (underlying, expiry)
        if cache_key not in expiry_price_cache:
            try:
                price_raw, is_finalized = oracle.functions.getExpiryPrice(
                    underlying, expiry
                ).call()
                expiry_price_cache[cache_key] = price_raw if is_finalized else None
            except Exception:
                logger.exception(
                    "Failed to read expiry price for %s at %d",
                    asset_str,
                    expiry,
                )
                expiry_price_cache[cache_key] = None

        oracle_price = expiry_price_cache[cache_key]
        if oracle_price is None:
            skipped.add((pos["user_address"], pos["vault_id"]))
            continue

        strike = int(pos["strike_price"])
        is_put = pos["is_put"]
        is_itm = (is_put and oracle_price < strike) or (
            not is_put and oracle_price > strike
        )
        if is_itm:
            pos["expiry_price_raw"] = oracle_price
            itm.append(pos)

    logger.info(
        "XLayer: %d ITM, %d skipped out of %d expired",
        len(itm),
        len(skipped),
        len(positions),
    )
    return itm, expiry_price_cache, skipped


def _compute_contra_amount(amount_raw, strike, is_put, asset="okb"):
    try:
        cfg = get_asset_config(Asset(asset))
    except (ValueError, KeyError):
        cfg = get_asset_config(Asset.OKB)
    underlying = Web3.to_checksum_address(cfg.underlying_address)
    usdc = Web3.to_checksum_address(settings.xlayer_usdc_address)
    scale = 10 ** (cfg.decimals - 8)

    if is_put:
        contra_amount = amount_raw * scale
        return contra_amount, usdc, underlying

    contra_amount = (amount_raw * strike) // (10**10)
    return contra_amount, underlying, usdc


def compute_slippage_param(position, oracle_price_8dec=None):
    amount_raw = int(position["amount"])
    strike = int(position["strike_price"])
    is_put = position["is_put"]
    asset_str = position.get("asset", "okb")

    contra_amount, _, _ = _compute_contra_amount(amount_raw, strike, is_put, asset_str)

    if not is_put:
        # CALL: minAmountOut
        buffer = (contra_amount * BETA_SLIPPAGE_BPS) // 10_000
        return contra_amount - buffer, contra_amount

    # PUT: maxCollateralSpent (beta mode — oracle price + buffer)
    if oracle_price_8dec is None:
        raise ValueError("oracle_price_8dec required for PUT")
    try:
        cfg = get_asset_config(Asset(asset_str))
    except (ValueError, KeyError):
        cfg = get_asset_config(Asset.OKB)
    divisor = 10 ** (cfg.decimals + 8 - 6)
    amount_in = (contra_amount * oracle_price_8dec) // divisor
    max_collateral = amount_in + (amount_in * BETA_SLIPPAGE_BPS + 9_999) // 10_000
    return max_collateral, contra_amount


def _db_update(user_addr, vault_id, fields, context):
    client = get_client()
    try:
        result = (
            client.table("order_events")
            .update(fields)
            .eq("user_address", user_addr.lower())
            .eq("vault_id", vault_id)
            .execute()
        )
        if not result.data:
            logger.error(
                "%s: matched no rows user=%s vault=%d",
                context,
                user_addr,
                vault_id,
            )
    except Exception:
        logger.exception(
            "%s: DB write failed user=%s vault=%d",
            context,
            user_addr,
            vault_id,
        )
        raise


def _ensure_expiry_prices_set(expiries):
    if not expiries:
        return
    oracle = get_xlayer_oracle()
    account = get_operator_account()

    for asset in [Asset.OKB]:
        cfg = get_asset_config(asset)
        underlying = Web3.to_checksum_address(cfg.underlying_address)

        try:
            chainlink_price, decimals, _ = get_asset_price_raw(asset)
            if decimals != 8:
                chainlink_price = int(chainlink_price * (10**8) / (10**decimals))
        except Exception:
            logger.exception(
                "Failed to read %s price, skipping expiry price set",
                asset.value,
            )
            continue

        for expiry in expiries:
            try:
                price_raw, is_finalized = oracle.functions.getExpiryPrice(
                    underlying, expiry
                ).call()
                if is_finalized:
                    continue
            except Exception:
                logger.exception(
                    "Failed to read expiry price for %s at %d",
                    asset.value,
                    expiry,
                )
                continue

            try:
                tx_fn = oracle.functions.setExpiryPrice(
                    underlying, expiry, chainlink_price
                )
                tx_hash = build_and_send_xlayer_tx(tx_fn, account)
                logger.info(
                    "XLayer set %s expiry price %d for %d, tx: %s",
                    asset.value,
                    chainlink_price,
                    expiry,
                    tx_hash,
                )
            except Exception as e:
                if "PriceAlreadySet" in str(e):
                    logger.info(
                        "Expiry price already set for %s at %d",
                        asset.value,
                        expiry,
                    )
                else:
                    logger.exception(
                        "Failed to set %s expiry price for %d",
                        asset.value,
                        expiry,
                    )


def _reconcile_settled_on_chain(positions):
    controller = get_xlayer_controller()
    remaining = []
    reconciled = 0

    for pos in positions:
        owner = Web3.to_checksum_address(pos["user_address"])
        vault_id = pos["vault_id"]
        try:
            settled = controller.functions.vaultSettled(owner, vault_id).call()
        except Exception:
            remaining.append(pos)
            continue

        if settled:
            now = datetime.now(timezone.utc).isoformat()
            try:
                _db_update(
                    pos["user_address"],
                    vault_id,
                    {"is_settled": True, "settled_at": now},
                    "XLayer reconcile",
                )
            except Exception:
                pass
            reconciled += 1
        else:
            remaining.append(pos)

    if reconciled:
        logger.warning("XLayer reconciled %d positions", reconciled)
    return remaining


async def _physical_redeem_with_retry(pos, settler, account, expiry_price_raw):
    max_retries = settings.settlement_max_retries
    otoken_addr = pos["otoken_address"]
    user_addr = pos["user_address"]
    mm_addr = pos["mm_address"]
    amount_raw = int(pos["amount"])
    vault_id = pos["vault_id"]

    for attempt in range(1, max_retries + 1):
        try:
            slippage_param, contra_amount = await asyncio.to_thread(
                compute_slippage_param,
                pos,
                expiry_price_raw,
            )
            tx_fn = settler.functions.physicalRedeem(
                Web3.to_checksum_address(otoken_addr),
                Web3.to_checksum_address(user_addr),
                amount_raw,
                slippage_param,
                Web3.to_checksum_address(mm_addr),
            )
            tx_hash = build_and_send_xlayer_tx(tx_fn, account)
            logger.info(
                "XLayer Phase 2: delivery %s vault %d attempt %d, tx: %s",
                user_addr,
                vault_id,
                attempt,
                tx_hash,
            )
            return tx_hash, contra_amount
        except Exception as exc:
            if attempt < max_retries:
                delay = _RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
                ]
                logger.warning(
                    "XLayer Phase 2 attempt %d/%d failed vault %d: %s",
                    attempt,
                    max_retries,
                    vault_id,
                    exc,
                )
                await asyncio.sleep(delay)
            else:
                raise


async def settle_once():
    positions = get_expired_unsettled()
    if not positions:
        logger.info("XLayer: no expired positions to settle")
        return

    try:
        positions = await asyncio.to_thread(_reconcile_settled_on_chain, positions)
    except Exception:
        logger.exception("XLayer reconciliation failed")
    if not positions:
        return

    expiries = {pos["expiry"] for pos in positions}
    await asyncio.to_thread(_ensure_expiry_prices_set, expiries)

    settler = get_xlayer_batch_settler()
    account = get_operator_account()
    settled_positions = []

    for i in range(0, len(positions), MAX_BATCH_SIZE):
        batch = positions[i : i + MAX_BATCH_SIZE]
        owners = [Web3.to_checksum_address(p["user_address"]) for p in batch]
        vault_ids = [p["vault_id"] for p in batch]

        try:
            tx_fn = settler.functions.batchSettleVaults(owners, vault_ids)
            tx_hash = build_and_send_xlayer_tx(tx_fn, account)
            logger.info(
                "XLayer Phase 1: settled %d vaults, tx: %s",
                len(batch),
                tx_hash,
            )
        except Exception:
            logger.exception("XLayer Phase 1 failed for %d vaults", len(batch))
            break

        settled_positions.extend(batch)
        now = datetime.now(timezone.utc).isoformat()
        client = get_client()
        for user_addr, vault_id in zip(owners, vault_ids):
            try:
                client.table("order_events").update(
                    {
                        "is_settled": True,
                        "settled_at": now,
                        "settlement_tx_hash": tx_hash,
                        "settlement_type": "cash",
                    }
                ).eq("user_address", user_addr.lower()).eq(
                    "vault_id", vault_id
                ).execute()
            except Exception:
                logger.exception(
                    "XLayer Phase 1 DB mark failed: %s vault %d",
                    user_addr,
                    vault_id,
                )

    if not settled_positions:
        return

    delay = settings.flash_loan_redeem_delay_seconds
    logger.info("XLayer waiting %ds before Phase 2", delay)
    await asyncio.sleep(delay)

    itm_positions, expiry_cache, skipped_keys = await asyncio.to_thread(
        identify_itm_positions,
        settled_positions,
    )

    usdc = settings.xlayer_usdc_address.lower()

    for pos in itm_positions:
        user_addr = pos["user_address"]
        mm_addr = pos.get("mm_address", "")
        vault_id = pos["vault_id"]
        expiry_price_raw = pos.get("expiry_price_raw")
        expiry_price_str = (
            str(expiry_price_raw) if expiry_price_raw is not None else None
        )

        if not mm_addr:
            logger.error(
                "XLayer: no mm_address for %s vault %d",
                user_addr,
                vault_id,
            )
            continue

        try:
            tx_hash, contra_amount = await _physical_redeem_with_retry(
                pos,
                settler,
                account,
                expiry_price_raw,
            )
        except Exception:
            logger.exception(
                "XLayer Phase 2 failed %s vault %d",
                user_addr,
                vault_id,
            )
            try:
                _db_update(
                    user_addr,
                    vault_id,
                    {
                        "settlement_type": "physical_failed",
                        "is_itm": True,
                        "expiry_price": expiry_price_str,
                    },
                    "XLayer Phase 2 failure mark",
                )
            except Exception:
                pass
            continue

        pos_cfg = get_asset_config(Asset.OKB)
        delivered_asset = pos_cfg.underlying_address.lower() if pos["is_put"] else usdc
        try:
            _db_update(
                user_addr,
                vault_id,
                {
                    "settlement_type": "physical",
                    "is_itm": True,
                    "expiry_price": expiry_price_str,
                    "delivery_tx_hash": tx_hash,
                    "delivered_asset": delivered_asset,
                    "delivered_amount": str(contra_amount),
                },
                "XLayer Phase 2 delivery mark",
            )
        except Exception:
            logger.exception(
                "XLayer delivery tx %s succeeded but DB failed",
                tx_hash,
            )

    # Mark OTM positions
    itm_keys = {(p["user_address"], p["vault_id"]) for p in itm_positions}
    excluded_keys = itm_keys | skipped_keys
    for pos in settled_positions:
        if (pos["user_address"], pos["vault_id"]) in excluded_keys:
            continue
        expiry = pos["expiry"]
        cfg = get_asset_config(Asset.OKB)
        cache_key = (Web3.to_checksum_address(cfg.underlying_address), expiry)
        cached_price = expiry_cache.get(cache_key)
        try:
            _db_update(
                pos["user_address"],
                pos["vault_id"],
                {
                    "is_itm": False,
                    "expiry_price": (
                        str(cached_price) if cached_price is not None else None
                    ),
                },
                "XLayer OTM mark",
            )
        except Exception:
            pass


async def _wait_until_target_hour():
    now = datetime.now(timezone.utc)
    target = now.replace(
        hour=settings.expiry_settle_hour_utc,
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    wait = (target - now).total_seconds() + 10
    logger.info("XLayer settler waiting %.0fs until %s", wait, target)
    await asyncio.sleep(wait)


async def _post_settle_sweep():
    interval = settings.settlement_sweep_interval_seconds
    max_cycles = settings.settlement_sweep_max_cycles
    for cycle in range(1, max_cycles + 1):
        await asyncio.sleep(interval)
        remaining = get_expired_unsettled()
        if not remaining:
            return
        logger.info(
            "XLayer sweep %d/%d: %d unsettled",
            cycle,
            max_cycles,
            len(remaining),
        )
        try:
            await settle_once()
        except Exception:
            logger.exception("XLayer sweep %d failed", cycle)


async def run():
    logger.info("XLayer expiry settler starting")

    try:
        await settle_once()
    except Exception:
        logger.exception("XLayer startup settlement failed")
    try:
        await _post_settle_sweep()
    except Exception:
        logger.exception("XLayer startup sweep failed")

    while True:
        await _wait_until_target_hour()
        try:
            await settle_once()
        except Exception:
            logger.exception("XLayer settlement failed")
        try:
            await _post_settle_sweep()
        except Exception:
            logger.exception("XLayer sweep failed")
