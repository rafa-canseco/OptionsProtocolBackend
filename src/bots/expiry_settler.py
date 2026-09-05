"""
Expiry Settler Bot

Two-phase settlement at 08:00 UTC daily:
  1. batchSettleVaults() — settles all expired vaults on-chain (collateral released)
  2. physicalRedeem() per ITM position — flash loan + DEX swap delivers contra-asset

DB marking happens per-batch in Phase 1 and per-position in Phase 2.
On-chain calls and DB writes are in separate try blocks to prevent misattribution.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from web3 import Web3

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import (
    get_batch_settler,
    get_controller,
    get_otoken,
    get_oracle,
    get_uniswap_quoter,
    get_operator_account,
    build_and_send_tx,
)
from src.chains import Chain
from src.pricing.assets import (
    Asset,
    get_base_settlement_asset,
    resolve_base_underlying,
)
from src.pricing.chainlink import get_asset_price_raw
from src.pricing.nvdac import is_us_regular_session
from src.settlement_routing import (
    MAX_ORACLE_AGE_SECONDS,
    NEW_SETTLEMENT_ASSETS,
    assert_route_unchanged,
    build_route_quote,
    read_new_asset_price_8,
    read_nvdac_close_price_8,
    validate_new_asset_participants,
)
from src.notifications.email import (
    build_consolidated_result_email,
    send_batch,
)

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 50  # max vaults per tx to avoid gas limit
BETA_SLIPPAGE_BPS = 1_000  # 10% buffer used in beta mode (no live DEX quote available)


def _position_asset_config(position_or_asset):
    asset = (
        position_or_asset.get("asset")
        if isinstance(position_or_asset, dict)
        else position_or_asset
    )
    if not isinstance(asset, str):
        raise ValueError(f"Missing Base settlement asset: {asset!r}")
    return get_base_settlement_asset(asset)


def _validate_position_asset(position: dict):
    """Bind stored asset metadata to the oToken's immutable underlying."""
    cfg = _position_asset_config(position)
    expected = cfg.underlying_address.lower()
    if position.get("_validated_underlying") == expected:
        return cfg
    otoken_address = position.get("otoken_address")
    if not isinstance(otoken_address, str) or not Web3.is_address(otoken_address):
        raise ValueError(f"Invalid oToken address: {otoken_address!r}")
    underlying = get_otoken(otoken_address).functions.underlying().call()
    resolved = resolve_base_underlying(underlying)
    if resolved.asset != cfg.asset or resolved.underlying_address.lower() != expected:
        raise ValueError(
            f"Stored/on-chain asset mismatch for {otoken_address}: "
            f"stored={cfg.asset} chain={resolved.asset}"
        )
    position["_validated_underlying"] = expected
    return cfg


_SETTLE_FIELDS = (
    "id, user_address, vault_id, otoken_address, expiry, amount, "
    "strike_price, is_put, mm_address, asset, is_settled, "
    "settlement_type, delivery_tx_hash, is_itm, settled_at, result_sent_at, "
    "premium, gross_premium, net_premium"
)


def get_expired_unsettled() -> list[dict]:
    """Get all unsettled positions with expired oTokens.

    Filters out rows missing settlement-critical fields (strike_price, is_put)
    which can happen if oToken metadata enrichment failed during indexing.
    """
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select(_SETTLE_FIELDS)
        .eq("chain", Chain.BASE.value)
        .or_("is_settled.eq.false,is_settled.is.null")
        .lte("expiry", now)
        .not_.is_("strike_price", "null")
        .not_.is_("is_put", "null")
        .not_.is_("amount", "null")
        .execute()
    )
    return result.data or []


def get_pending_phase2() -> list[dict]:
    """Positions whose Phase 1 (cash settle) is done in DB but Phase 2
    (physical delivery) is missing.

    Recovers from crashes between Phase 1 and Phase 2 — without this, such
    positions would never get their delivery because get_expired_unsettled
    filters them out (is_settled is already True). Excludes positions that
    are known OTM (no delivery needed) or already in a terminal failed state.
    """
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select(_SETTLE_FIELDS)
        .eq("chain", Chain.BASE.value)
        .eq("is_settled", True)
        .is_("delivery_tx_hash", "null")
        .lte("expiry", now)
        .or_("is_itm.is.null,is_itm.eq.true")
        .or_("settlement_type.is.null,settlement_type.eq.cash")
        .not_.is_("strike_price", "null")
        .not_.is_("is_put", "null")
        .not_.is_("amount", "null")
        .not_.is_("mm_address", "null")
        .execute()
    )
    return result.data or []


def identify_itm_positions(
    positions: list[dict],
) -> tuple[list[dict], dict[int, int | None], set[tuple[str, int]]]:
    """Separate ITM from OTM positions based on oracle expiry price.

    Reads the oracle's finalized expiry price and compares with strike:
      - PUT is ITM if expiryPrice < strikePrice
      - CALL is ITM if expiryPrice > strikePrice

    Returns (itm_positions, expiry_price_cache, skipped_keys):
      - itm_positions: positions that are in-the-money
      - expiry_price_cache: reusable cache for OTM marking
      - skipped_keys: (user_address, vault_id) tuples for positions whose
        oracle price was unavailable — must be excluded from OTM classification
    """
    oracle = get_oracle()

    itm: list[dict] = []
    skipped: set[tuple[str, int]] = set()
    # Cache keyed by (underlying_address, expiry)
    expiry_price_cache: dict[tuple[str, int], int | None] = {}

    for pos in positions:
        expiry = pos["expiry"]
        asset_str = pos.get("asset")
        try:
            cfg = _validate_position_asset(pos)
        except (ValueError, TypeError) as exc:
            logger.error(
                "Unknown settlement asset; skipping oracle/physical delivery "
                "id=%s oToken=%s user=%s vault=%s asset=%r error=%s",
                pos.get("id"),
                pos.get("otoken_address"),
                pos.get("user_address"),
                pos.get("vault_id"),
                asset_str,
                exc,
            )
            skipped.add((pos["user_address"], pos["vault_id"]))
            continue
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
                    "Failed to read expiry price for %s at %d", asset_str, expiry
                )
                expiry_price_cache[cache_key] = None

        oracle_price = expiry_price_cache[cache_key]
        if oracle_price is None:
            logger.warning(
                f"Expiry price not finalized for {expiry}, skipping position"
            )
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
        f"Identified {len(itm)} ITM, {len(skipped)} skipped "
        f"out of {len(positions)} expired positions"
    )
    return itm, expiry_price_cache, skipped


def _compute_contra_amount(
    amount_raw: int, strike: int, is_put: bool, asset: str
) -> tuple[int, str, str]:
    """Determine contra-asset amount and token direction.

    Returns (contra_amount, token_in_addr, token_out_addr).

    oToken amounts are always 8 decimals. The scaling factor to reach
    the underlying's native decimals varies by asset:
      - ETH (18 dec): scale = 10^10  (10^8 * 10^10 = 10^18)
      - BTC (8 dec):  scale = 10^0   (10^8 * 1 = 10^8)

    PUT ITM:  user gets underlying. contra = amount * scale
    CALL ITM: user gets USDC.      contra = amount * strike / 10^10
    """
    cfg = _position_asset_config(asset)
    underlying = Web3.to_checksum_address(cfg.underlying_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    # oToken is 8 dec, underlying is cfg.decimals dec
    scale = 10 ** (cfg.decimals - 8)

    if is_put:
        contra_amount = amount_raw * scale
        return contra_amount, usdc, underlying

    contra_amount = (amount_raw * strike) // (10**10)
    if contra_amount == 0:
        logger.warning(
            "CALL contra_amount truncated to 0 (dust position): amount_raw=%d strike=%d",
            amount_raw,
            strike,
        )
    return contra_amount, underlying, usdc


def _beta_compute_max_collateral_put(
    contra_amount: int, oracle_price_8dec: int, underlying_decimals: int = 18
) -> int:
    """Compute maxCollateralSpent for PUT physicalRedeem (beta mode).

    No Uniswap Quoter needed — converts underlying contra amount to USDC
    using oracle price with a 10% buffer.

    oracle_price_8dec is asset/USD in 8 decimals.
    """
    if oracle_price_8dec <= 0:
        raise ValueError(f"oracle_price_8dec must be positive, got {oracle_price_8dec}")

    # PUT: collateral is USDC (6-dec), contra is underlying (N-dec)
    # max_collateral_usdc = contra * oracle_price / 10^(N + 8 - 6)
    divisor = 10 ** (underlying_decimals + 8 - 6)
    amount_in = (contra_amount * oracle_price_8dec) // divisor

    # BETA_SLIPPAGE_BPS ceiling buffer: (amount * bps + 9999) // 10000 rounds up
    max_collateral = amount_in + (amount_in * BETA_SLIPPAGE_BPS + 9_999) // 10_000
    logger.info(
        f"Beta PUT swap estimate: {amount_in} → max {max_collateral} (10% buffer)"
    )
    return max_collateral


def _compute_min_amount_out(contra_amount: int) -> int:
    """Compute minAmountOut for CALL physicalRedeem.

    contra_amount is the exact USDC the user should receive (the expected swap
    output). Returns a lower bound with a downside slippage buffer so the
    contract reverts if the DEX returns too little.

    Beta mode: BETA_SLIPPAGE_BPS (10%) downside buffer.
    Production: swap_slippage_tolerance downside.

    contra_amount is the exact USDC owed to the user (strike × oToken amount in
    the correct decimals). The Uniswap Quoter is not needed here: for CALLs the
    swap target is exactly contra_amount USDC, so the expected output equals
    contra_amount and the floor is contra_amount minus the slippage buffer.
    """
    if contra_amount <= 0:
        raise ValueError(
            f"contra_amount must be positive for CALL minAmountOut, got {contra_amount}. "
            "Check amount_raw and strike_price for this position."
        )
    if settings.beta_mode:
        buffer = (contra_amount * BETA_SLIPPAGE_BPS) // 10_000
    else:
        slippage_bps = int(settings.swap_slippage_tolerance * 10_000)
        buffer = (contra_amount * slippage_bps) // 10_000
    min_amount_out = contra_amount - buffer
    logger.info(
        f"CALL minAmountOut: {contra_amount} → min {min_amount_out} (slippage buffer)"
    )
    return min_amount_out


def compute_slippage_param(
    position: dict,
    oracle_price_8dec: int | None = None,
) -> tuple[int, int]:
    """Compute the slippage param for physicalRedeem.

    Dual semantics per B1N-171:
    - PUT:  returns maxCollateralSpent (max USDC input for USDC→WETH swap)
    - CALL: returns minAmountOut (min USDC output from WETH→USDC swap)

    In beta mode (settings.beta_mode=True), PUT uses Oracle price + 10% buffer
    instead of the Uniswap Quoter. CALL always derives from contra_amount.

    Returns (slippage_param, contra_amount).
    """
    amount_raw = int(position["amount"])  # 8 decimals (oToken)
    strike = int(position["strike_price"])  # 8 decimals
    is_put = position["is_put"]

    if amount_raw <= 0:
        raise ValueError(
            f"amount_raw must be positive, got {amount_raw} "
            f"for oToken {position.get('otoken_address')}"
        )
    if strike <= 0:
        raise ValueError(
            f"strike_price must be positive, got {strike} "
            f"for oToken {position.get('otoken_address')}"
        )

    asset_str = position.get("asset")
    contra_amount, token_in, token_out = _compute_contra_amount(
        amount_raw, strike, is_put, asset_str
    )

    # CALL: minAmountOut — contra_amount IS the expected USDC output, apply downside buffer
    if not is_put:
        return _compute_min_amount_out(contra_amount), contra_amount

    # PUT: maxCollateralSpent
    # Beta mode: use Oracle price instead of Quoter
    if settings.beta_mode:
        if oracle_price_8dec is None:
            raise ValueError(
                "oracle_price_8dec is required in beta mode (no Uniswap Quoter available)"
            )
        cfg = _position_asset_config(asset_str)
        max_collateral = _beta_compute_max_collateral_put(
            contra_amount, oracle_price_8dec, cfg.decimals
        )
        return max_collateral, contra_amount

    # Production mode: Uniswap Quoter for exact WETH output quote
    quoter = get_uniswap_quoter()
    try:
        result = quoter.functions.quoteExactOutputSingle(
            (token_in, token_out, contra_amount, settings.uniswap_fee_tier, 0)
        ).call()
        amount_in = result[0]
    except Exception:
        logger.exception(
            f"Quoter failed for {position['otoken_address']}, "
            f"in={token_in} out={token_out} amount={contra_amount}"
        )
        raise

    # Integer arithmetic: avoid float for amounts that can exceed 2^53
    slippage_bps = int(settings.swap_slippage_tolerance * 10_000)
    max_collateral = amount_in + (amount_in * slippage_bps + 9_999) // 10_000
    logger.info(
        f"PUT swap quote: {amount_in} → max {max_collateral} "
        f"(slippage {settings.swap_slippage_tolerance:.1%}) "
        f"for oToken {position['otoken_address']}"
    )
    return max_collateral, contra_amount


def _db_update(user_addr: str, vault_id: int, fields: dict, context: str) -> None:
    """Update a single order_events row. Logs on no-match or failure."""
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
                f"{context}: matched no rows user={user_addr} vault={vault_id}"
            )
    except Exception:
        logger.exception(
            f"{context}: DB write failed user={user_addr} vault={vault_id}"
        )
        raise


def _db_update_by_id(order_event_id: str, fields: dict, context: str) -> None:
    """Update a single order_events row by primary key.

    Raises on no-match so callers (e.g. dedup marking after an email was
    already sent) treat it as a failure and can alert/retry instead of
    silently skipping the mark.
    """
    client = get_client()
    try:
        result = (
            client.table("order_events")
            .update(fields)
            .eq("id", order_event_id)
            .execute()
        )
    except Exception:
        logger.exception(
            "%s: DB write failed order_event_id=%s", context, order_event_id
        )
        raise
    if not result.data:
        raise RuntimeError(
            f"{context}: matched no rows for order_event_id={order_event_id}"
        )


def _ensure_expiry_prices_set(
    required: set[tuple[str, int]], *, now: int | None = None
) -> None:
    """Submit only required asset/expiry prices to the Base Oracle."""
    if not required:
        return
    oracle = get_oracle()
    account = get_operator_account()
    now = now or int(datetime.now(timezone.utc).timestamp())
    for asset_name, expiry in sorted(required):
        try:
            cfg = _position_asset_config(asset_name)
            underlying = Web3.to_checksum_address(cfg.underlying_address)
            price_raw, is_finalized = oracle.functions.getExpiryPrice(
                underlying, expiry
            ).call()
            if is_finalized:
                logger.info(
                    "Expiry price already set for %s at %d: %d",
                    asset_name,
                    expiry,
                    price_raw,
                )
                continue
            close_price = None
            expiry_at = datetime.fromtimestamp(expiry, timezone.utc)
            if (
                asset_name != "nvdac"
                and not oracle.functions.legacyPostExpiryAsset(underlying).call()
            ):
                raise ValueError("Oracle legacy settlement is not allowlisted")
            if asset_name == "nvdac" and not is_us_regular_session(expiry_at):
                close_price = read_nvdac_close_price_8(expiry, now=now)
                chainlink_price = close_price.price_8
            elif asset_name in NEW_SETTLEMENT_ASSETS:
                chainlink_price = read_new_asset_price_8(
                    asset_name,
                    not_before=expiry,
                    not_after=expiry + MAX_ORACLE_AGE_SECONDS,
                    now=now if asset_name == "nvdac" else None,
                    apply_multiplier=asset_name != "nvdac",
                )
            else:
                legacy_asset = Asset(asset_name)
                chainlink_price, decimals, _ = get_asset_price_raw(legacy_asset)
                if decimals != 8:
                    chainlink_price = chainlink_price * (10**8) // (10**decimals)
            if close_price is not None:
                configured_close_at, configured_next_open_at = (
                    oracle.functions.closeWindow(underlying, expiry).call()
                )
                if (
                    int(configured_close_at) != close_price.close_at
                    or int(configured_next_open_at) != close_price.next_session_open_at
                ):
                    raise ValueError("NVDA Oracle close window is not precommitted")
                tx_fn = oracle.functions.setExpiryPriceFromCloseAtRound(
                    underlying,
                    expiry,
                    chainlink_price,
                    close_price.close_at,
                    close_price.next_session_open_at,
                    close_price.round_id,
                )
            else:
                tx_fn = oracle.functions.setExpiryPrice(
                    underlying, expiry, chainlink_price
                )
            tx_hash = build_and_send_tx(tx_fn, account)
            logger.info(
                "Set %s expiry price %d for expiry %d, tx: %s",
                asset_name,
                chainlink_price,
                expiry,
                tx_hash,
            )
        except Exception as exc:
            if "PriceAlreadySet" in str(exc):
                logger.info(
                    "Expiry price already set for %s at %d (race)",
                    asset_name,
                    expiry,
                )
            else:
                logger.exception(
                    "Failed closed setting expiry price for %s at %d",
                    asset_name,
                    expiry,
                )


_RETRY_BACKOFF_SECONDS = [60, 300, 900, 1800, 3600]  # 1m, 5m, 15m, 30m, 60m


async def _physical_redeem_with_retry(
    pos: dict,
    settler,
    account,
    expiry_price_raw: int | None,
) -> tuple[str, int]:
    """Attempt physical delivery with exponential backoff retry.

    Returns (tx_hash, contra_amount) on success.
    Raises after all retries are exhausted.
    """
    max_retries = settings.settlement_max_retries
    if max_retries < 1:
        raise ValueError(f"settlement_max_retries must be >= 1, got {max_retries}")
    otoken_addr = pos["otoken_address"]
    user_addr = pos["user_address"]
    mm_addr = pos["mm_address"]
    amount_raw = int(pos["amount"])
    vault_id = pos["vault_id"]

    for attempt in range(1, max_retries + 1):
        try:
            asset = pos.get("asset")
            if asset in NEW_SETTLEMENT_ASSETS:
                amount_raw = int(pos["amount"])
                strike = int(pos["strike_price"])
                contra_amount, _, _ = _compute_contra_amount(
                    amount_raw, strike, bool(pos["is_put"]), asset
                )
                route_quote = await asyncio.to_thread(
                    build_route_quote, pos, contra_amount
                )
                slippage_param = route_quote.slippage_param
                await asyncio.to_thread(
                    assert_route_unchanged, pos, route_quote.fingerprint
                )
            else:
                slippage_param, contra_amount = await asyncio.to_thread(
                    compute_slippage_param,
                    pos,
                    expiry_price_raw,
                )
            if slippage_param <= 0:
                raise ValueError(f"slippage_param={slippage_param} for {otoken_addr}")

            tx_fn = settler.functions.physicalRedeem(
                Web3.to_checksum_address(otoken_addr),
                Web3.to_checksum_address(user_addr),
                amount_raw,
                slippage_param,
                Web3.to_checksum_address(mm_addr),
            )
            tx_hash = build_and_send_tx(tx_fn, account)
            logger.info(
                "Phase 2: delivery for %s vault %d (attempt %d/%d), tx: %s",
                user_addr,
                vault_id,
                attempt,
                max_retries,
                tx_hash,
            )
            return tx_hash, contra_amount
        except Exception as exc:
            if attempt < max_retries:
                delay = _RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
                ]
                logger.warning(
                    "Phase 2 attempt %d/%d failed for %s vault %d: %s. Retrying in %ds",
                    attempt,
                    max_retries,
                    user_addr,
                    vault_id,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "ALERT: All %d retries exhausted for %s vault %d. "
                    "Position requires manual intervention. "
                    "Last error: %s",
                    max_retries,
                    user_addr,
                    vault_id,
                    exc,
                )
                raise


def _reconcile_settled_on_chain(
    positions: list[dict], *, update_db: bool = True
) -> tuple[list[dict], list[dict]]:
    """Check on-chain settlement state and reconcile DB for any mismatches.

    Returns (unsettled_on_chain, already_settled_on_chain). Positions that
    are already settled on-chain are marked is_settled=True in DB and
    returned in the second list so the caller can skip Phase 1 (which
    would revert with VaultAlreadySettled) but still feed them into
    Phase 2 (physical delivery). Without this, an ITM vault whose Phase 1
    landed but whose Phase 2 didn't (e.g. operator ran out of gas) would
    be silently dropped from the queue and the delivery never executed.
    """
    controller = get_controller()
    unsettled: list[dict] = []
    already_settled: list[dict] = []
    db_failures = 0

    for pos in positions:
        owner = Web3.to_checksum_address(pos["user_address"])
        vault_id = pos["vault_id"]
        try:
            settled = controller.functions.vaultSettled(owner, vault_id).call()
        except Exception:
            logger.warning(
                "Could not check vaultSettled for user=%s vault=%d, assuming unsettled",
                pos["user_address"],
                vault_id,
            )
            unsettled.append(pos)
            continue

        if settled:
            if update_db:
                now = datetime.now(timezone.utc).isoformat()
                try:
                    _db_update(
                        pos["user_address"],
                        vault_id,
                        {"is_settled": True, "settled_at": now},
                        "Reconcile on-chain settled",
                    )
                except Exception:
                    logger.exception(
                        "ALERT: Reconcile DB write failed for user=%s "
                        "vault=%d (settled on-chain)",
                        pos["user_address"],
                        vault_id,
                    )
                    db_failures += 1
            already_settled.append(pos)
        else:
            unsettled.append(pos)

    if already_settled and update_db:
        msg = "Reconciled %d positions (settled on-chain but not in DB)"
        if db_failures:
            msg += " — %d DB writes failed, will retry next cycle"
            logger.warning(msg, len(already_settled), db_failures)
        else:
            logger.warning(msg, len(already_settled))
    return unsettled, already_settled


def _format_position_for_email(
    pos: dict,
    wallet: str,
    itm_keys: set[tuple[str, int]],
) -> dict:
    """Format a settled position into a dict for render_result_email_consolidated."""
    vault_id = pos["vault_id"]
    asset = str(pos.get("asset") or "UNKNOWN").upper()
    strike_raw = int(pos.get("strike_price", 0))
    strike_usd = f"{strike_raw / 1e8:,.0f}"
    amount_raw = int(pos.get("amount", 0))
    amount_human = f"{amount_raw / 1e8:.4f}"
    _premium = pos.get("net_premium") or pos.get("premium")
    if _premium is None:
        logger.warning(
            "No premium field for %s vault %d — email will show $0.00", wallet, vault_id
        )
    premium_usd = f"{int(_premium or 0) / 1e6:.2f}"
    collateral_raw = amount_raw * strike_raw // 10**8
    collateral_usd = f"{collateral_raw / 1e6:,.0f}"
    return {
        "asset": asset,
        "strike_usd": strike_usd,
        "is_itm": (wallet, vault_id) in itm_keys,
        "option_type": "put" if pos.get("is_put", True) else "call",
        "amount": amount_human,
        "collateral_usd": collateral_usd,
        "premium_usd": premium_usd,
    }


def _prepare_settlement_email_batch(
    all_positions: list[dict],
    email_map: dict[str, str],
    itm_keys: set[tuple[str, int]],
) -> tuple[list[dict], list[list[str]]]:
    """Build one consolidated email per wallet covering all their settled positions.

    Returns (emails_to_send, position_refs) where position_refs[i] is the list
    of order_events.id values covered by emails_to_send[i]. result_sent_at is
    marked per individual position so dedup stays correct on retry.
    itm_positions must be a subset of all_positions (i.e. settled_positions).
    """
    by_wallet: dict[str, list[dict]] = {}
    for pos in all_positions:
        wallet = pos["user_address"]
        if not email_map.get(wallet):
            continue
        if pos.get("result_sent_at"):
            logger.debug(
                "Skipping result email for %s vault %d (already sent, order_event_id=%s)",
                wallet,
                pos["vault_id"],
                pos["id"],
            )
            continue
        by_wallet.setdefault(wallet, []).append(pos)

    emails_to_send: list[dict] = []
    position_refs: list[list[str]] = []

    for wallet, positions in by_wallet.items():
        formatted = [_format_position_for_email(p, wallet, itm_keys) for p in positions]
        refs = [p["id"] for p in positions]
        try:
            email_dict = build_consolidated_result_email(
                email=email_map[wallet],
                wallet_address=wallet,
                positions=formatted,
            )
            emails_to_send.append(email_dict)
            position_refs.append(refs)
        except Exception:
            logger.exception("Failed to build consolidated result email for %s", wallet)

    return emails_to_send, position_refs


def _send_settlement_emails(
    settled_positions: list[dict],
    itm_positions: list[dict],
) -> None:
    """Send settlement result emails (fire-and-forget).

    Queries user_emails for verified/subscribed wallets, builds
    OTM or ITM result emails, sends via Resend batch.
    itm_positions must be a subset of settled_positions.
    """
    if not settings.resend_api_key:
        return

    wallets = list({p["user_address"] for p in settled_positions})
    if not wallets:
        return

    client = get_client()
    try:
        result = (
            client.table("user_emails")
            .select("wallet_address, email")
            .in_("wallet_address", wallets)
            .not_.is_("verified_at", "null")
            .is_("unsubscribed_at", "null")
            .execute()
        )
        email_map = {row["wallet_address"]: row["email"] for row in (result.data or [])}
    except Exception:
        logger.exception("Failed to fetch user emails for settlement results")
        return

    if not email_map:
        return

    itm_keys = {(p["user_address"], p["vault_id"]) for p in itm_positions}
    emails_to_send, position_refs = _prepare_settlement_email_batch(
        settled_positions, email_map, itm_keys
    )

    if not emails_to_send:
        return

    # Fail closed: mark before sending. If the email provider succeeds but the
    # DB mark fails afterward, users get duplicate result emails on every
    # restart/cycle. Missing a retry is less harmful than spamming old positions.
    now = datetime.now(timezone.utc).isoformat()
    marked_emails: list[dict] = []
    marked_refs: list[list[str]] = []
    failed_marks: list[str] = []
    for email_dict, refs in zip(emails_to_send, position_refs):
        try:
            for order_event_id in refs:
                _db_update_by_id(
                    order_event_id, {"result_sent_at": now}, "Settlement email pre-mark"
                )
        except Exception:
            logger.exception(
                "Settlement email pre-mark failed for refs=%s; skipping send",
                refs,
            )
            failed_marks.extend(refs)
            continue
        marked_emails.append(email_dict)
        marked_refs.append(refs)

    if failed_marks:
        logger.error(
            "ALERT: skipped %d settlement email position(s) because result_sent_at "
            "could not be marked. order_event_ids=%s",
            len(failed_marks),
            failed_marks,
        )

    if not marked_emails:
        return

    logger.info("Sending %d settlement result emails", len(marked_emails))
    try:
        results = send_batch(marked_emails)
    except Exception:
        logger.exception(
            "Settlement result batch send failed after pre-mark; will not retry "
            "automatically to avoid duplicate result emails"
        )
        return

    if len(results) != len(marked_emails):
        logger.error(
            "Settlement batch results length mismatch: sent=%d got=%d",
            len(marked_emails),
            len(results),
        )

    for i, refs in enumerate(marked_refs):
        if i >= len(results) or not results[i].get("id"):
            logger.warning(
                "Settlement email send reported no id for pre-marked refs=%s; "
                "not retrying automatically to avoid duplicates",
                refs,
            )


def _preflight_positions(positions: list[dict]) -> list[dict]:
    """Exclude positions whose live identity or B20 actors cannot be proven."""
    valid: list[dict] = []
    for pos in positions:
        try:
            cfg = _validate_position_asset(pos)
            if cfg.asset in NEW_SETTLEMENT_ASSETS and not bool(pos["is_put"]):
                validate_new_asset_participants(
                    cfg.asset, (Web3.to_checksum_address(pos["user_address"]),)
                )
            valid.append(pos)
        except Exception:
            logger.exception(
                "Fail-closed settlement preflight id=%s oToken=%r asset=%r",
                pos.get("id"),
                pos.get("otoken_address"),
                pos.get("asset"),
            )
    return valid


async def settle_once():
    """Single settlement cycle: 2-phase (batch settle + physical delivery for ITM)."""
    positions = get_expired_unsettled()
    phase2_recovery = get_pending_phase2()
    if not positions and not phase2_recovery:
        logger.info("No expired positions to settle")
        return

    positions = await asyncio.to_thread(_preflight_positions, positions)
    phase2_recovery = await asyncio.to_thread(_preflight_positions, phase2_recovery)
    if not positions and not phase2_recovery:
        logger.error("No positions passed settlement identity/policy preflight")
        return

    # --- Reconcile: check on-chain state for DB/chain mismatches ---
    reconciled_already_settled: list[dict] = []
    if positions:
        try:
            positions, reconciled_already_settled = await asyncio.to_thread(
                _reconcile_settled_on_chain, positions
            )
        except Exception:
            logger.exception("Reconciliation failed, proceeding with all positions")

    # Phase-2-pending positions feed directly into Phase 2 — both those that
    # were is_settled=True from a prior crashed cycle (phase2_recovery) and
    # those just observed as on-chain-settled by the reconcile pass.
    phase2_only_seed: list[dict] = list(reconciled_already_settled)
    seen_keys = {(p["user_address"], p["vault_id"]) for p in phase2_only_seed}
    for pos in phase2_recovery:
        key = (pos["user_address"], pos["vault_id"])
        if key not in seen_keys:
            phase2_only_seed.append(pos)
            seen_keys.add(key)

    if phase2_only_seed:
        logger.warning(
            "Phase 2 recovery: %d positions had Phase 1 settled on-chain but "
            "no delivery — will run Phase 2 only",
            len(phase2_only_seed),
        )

    if not positions and not phase2_only_seed:
        logger.info("All positions reconciled (already settled and delivered)")
        return

    # --- Phase 0: set expiry prices on Oracle from Chainlink ---
    # Include phase2-only positions: identify_itm needs their oracle prices too.
    required_expiry_prices: set[tuple[str, int]] = set()
    for pos in [*positions, *phase2_only_seed]:
        try:
            cfg = _validate_position_asset(pos)
            required_expiry_prices.add((cfg.asset, int(pos["expiry"])))
        except (ValueError, TypeError):
            logger.exception(
                "Unknown settlement asset before expiry-price work "
                "id=%s oToken=%s user=%s vault=%s asset=%r",
                pos.get("id"),
                pos.get("otoken_address"),
                pos.get("user_address"),
                pos.get("vault_id"),
                pos.get("asset"),
            )
    await asyncio.to_thread(_ensure_expiry_prices_set, required_expiry_prices)

    # --- Phase 1: batchSettleVaults (settles all expired vaults on-chain) ---
    settler = get_batch_settler()
    account = get_operator_account()

    # Phase-2-only seed bypasses Phase 1 (already settled on-chain).
    settled_positions: list[dict] = list(phase2_only_seed)
    email_eligible_positions: list[dict] = []
    phase1_failed = False

    for i in range(0, len(positions), MAX_BATCH_SIZE):
        batch = positions[i : i + MAX_BATCH_SIZE]
        owners = [Web3.to_checksum_address(p["user_address"]) for p in batch]
        vault_ids = [p["vault_id"] for p in batch]

        # Step 1: on-chain settlement
        try:
            tx_fn = settler.functions.batchSettleVaults(owners, vault_ids)
            tx_hash = build_and_send_tx(tx_fn, account)
            logger.info(f"Phase 1: settled {len(batch)} vaults on-chain, tx: {tx_hash}")
        except Exception:
            logger.exception(
                "Phase 1: batchSettleVaults tx failed for %d vaults, "
                "reconciling batch on-chain",
                len(batch),
            )
            # Check which vaults are already settled on-chain
            unsettled, already_settled = _reconcile_settled_on_chain(batch)
            if already_settled:
                settled_positions.extend(already_settled)
            if unsettled:
                logger.error(
                    "Phase 1: %d/%d vaults unsettled after "
                    "reconciliation, aborting batch loop",
                    len(unsettled),
                    len(batch),
                )
                phase1_failed = True
                break
            logger.info(
                "Phase 1: all %d vaults in failed batch were already settled on-chain",
                len(batch),
            )
            continue

        # BatchSettler catches individual vault failures, so confirm each result.
        unsettled, confirmed = _reconcile_settled_on_chain(batch, update_db=False)
        if unsettled:
            logger.error(
                "Phase 1: %d/%d vaults emitted/retained an individual failure",
                len(unsettled),
                len(batch),
            )
            phase1_failed = True
        if not confirmed:
            continue
        settled_positions.extend(confirmed)

        # Step 2: mark confirmed vaults in DB.
        confirmed_owners = [
            Web3.to_checksum_address(p["user_address"]) for p in confirmed
        ]
        confirmed_vault_ids = [p["vault_id"] for p in confirmed]
        now = datetime.now(timezone.utc).isoformat()
        try:
            _mark_batch_settled(confirmed_owners, confirmed_vault_ids, tx_hash, now)
            for pos in confirmed:
                pos["is_settled"] = True
                pos["settled_at"] = now
                pos["settlement_tx_hash"] = tx_hash
            email_eligible_positions.extend(confirmed)
        except Exception:
            logger.exception(
                f"Phase 1: DB write failed after on-chain success (tx: {tx_hash}). "
                f"{len(confirmed)} vaults settled on-chain but not marked in DB."
            )
            # Continue — vaults are in settled_positions so Phase 2 can still run

    if not settled_positions:
        logger.error("Phase 1: no batches settled successfully, aborting")
        return

    new_settled = len(settled_positions) - len(phase2_only_seed)
    if phase1_failed:
        logger.warning(
            f"Phase 1: partial success — {new_settled}/{len(positions)} "
            f"vaults settled this cycle. Continuing with settled vaults only."
        )

    # --- Wait for vaults to be fully settled before physical delivery ---
    # Only relevant for vaults JUST settled in this cycle. Phase-2-only
    # recovery vaults were settled long ago and don't need this delay.
    if new_settled > 0:
        delay = settings.flash_loan_redeem_delay_seconds
        logger.info(f"Waiting {delay}s before physical delivery phase")
        await asyncio.sleep(delay)

    # --- Phase 2: physical delivery for ITM positions ---
    itm_positions, expiry_cache, skipped_keys = await asyncio.to_thread(
        identify_itm_positions,
        settled_positions,
    )

    usdc = settings.usdc_address.lower()

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
                "ALERT: No mm_address for user=%s vault=%d. "
                "Cannot do physical delivery.",
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
                    "Phase 2 missing-mm mark",
                )
            except Exception:
                logger.exception(
                    "ALERT: Failed to mark physical_failed for "
                    "user=%s vault=%d (missing mm_address). "
                    "Position has no settlement_type in DB.",
                    user_addr,
                    vault_id,
                )
            continue

        # Physical delivery with retry (slippage + on-chain tx)
        try:
            tx_hash, contra_amount = await _physical_redeem_with_retry(
                pos,
                settler,
                account,
                expiry_price_raw,
            )
        except Exception:
            logger.exception(
                "Phase 2 delivery failed for user=%s vault=%d after retries exhausted",
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
                    "Phase 2 all-retries-exhausted mark",
                )
            except Exception:
                logger.error(
                    "ALERT: Failed to mark physical_failed for "
                    "user=%s vault=%d after all retries exhausted.",
                    user_addr,
                    vault_id,
                )
            continue

        # DB mark (separate from on-chain to prevent misattribution)
        pos_cfg = _position_asset_config(pos)
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
                "Phase 2 delivery mark",
            )
        except Exception:
            logger.exception(
                "ALERT: Physical delivery succeeded on-chain "
                "(tx: %s) but DB write failed for user=%s vault=%d. "
                "DB retains settlement_type='cash' from Phase 1.",
                tx_hash,
                user_addr,
                vault_id,
            )

    # Update OTM positions with expiry price and ITM flag (display only).
    # Exclude both ITM positions and skipped positions (oracle unavailable)
    # to avoid incorrectly marking skipped positions as OTM.
    itm_keys = {(p["user_address"], p["vault_id"]) for p in itm_positions}
    excluded_keys = itm_keys | skipped_keys
    otm_positions = [
        p
        for p in settled_positions
        if (p["user_address"], p["vault_id"]) not in excluded_keys
    ]
    if otm_positions:
        otm_failures = 0
        for pos in otm_positions:
            expiry = pos["expiry"]
            try:
                pos_cfg = _position_asset_config(pos)
            except ValueError:
                logger.exception(
                    "Unknown settlement asset during OTM marking id=%s asset=%r",
                    pos.get("id"),
                    pos.get("asset"),
                )
                otm_failures += 1
                continue
            cache_key = (Web3.to_checksum_address(pos_cfg.underlying_address), expiry)
            cached_price = expiry_cache.get(cache_key)
            expiry_price_str = str(cached_price) if cached_price is not None else None
            try:
                _db_update(
                    pos["user_address"],
                    pos["vault_id"],
                    {
                        "is_itm": False,
                        "expiry_price": expiry_price_str,
                    },
                    "OTM expiry price update",
                )
            except Exception:
                otm_failures += 1
        if otm_failures:
            logger.error(
                f"Failed to update {otm_failures}/{len(otm_positions)} OTM positions"
            )
        else:
            logger.info(f"Updated {len(otm_positions)} OTM positions with expiry data")
    if skipped_keys:
        logger.error(
            f"ALERT: {len(skipped_keys)} positions skipped (oracle unavailable). "
            f"Already settled on-chain but lack ITM/OTM classification. "
            f"REQUIRES MANUAL REVIEW."
        )

    # --- Email notifications (fire-and-forget) ---
    try:
        await asyncio.to_thread(
            _send_settlement_emails, email_eligible_positions, itm_positions
        )
    except Exception:
        logger.exception("Settlement emails failed (non-blocking)")


def _mark_batch_settled(
    owners: list[str],
    vault_ids: list[int],
    tx_hash: str,
    now: str,
) -> None:
    """Mark a batch of positions as settled in the DB. Raises if any failed."""
    client = get_client()
    failures = 0
    for user_addr, vault_id in zip(owners, vault_ids):
        try:
            result = (
                client.table("order_events")
                .update(
                    {
                        "is_settled": True,
                        "settled_at": now,
                        "settlement_tx_hash": tx_hash,
                        "settlement_type": "cash",
                    }
                )
                .eq("user_address", user_addr.lower())
                .eq("vault_id", vault_id)
                .execute()
            )
            if not result.data:
                logger.error(
                    f"_mark_batch_settled matched no rows: user={user_addr} vault={vault_id}"
                )
                failures += 1
        except Exception:
            logger.exception(
                f"_mark_batch_settled failed: user={user_addr} vault={vault_id}"
            )
            failures += 1
    if failures:
        raise RuntimeError(
            f"_mark_batch_settled: {failures}/{len(owners)} positions failed"
        )


async def _wait_until_target_hour():
    """Sleep until the next settlement hour (default 08:00 UTC)."""
    now = datetime.now(timezone.utc)
    target = now.replace(
        hour=settings.expiry_settle_hour_utc,
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)

    wait_seconds = (target - now).total_seconds() + 10  # 10s buffer past the hour
    logger.info(
        f"Expiry settler waiting {wait_seconds:.0f}s until {target.isoformat()} +10s"
    )
    await asyncio.sleep(wait_seconds)


async def _post_settle_sweep():
    """Sweep for remaining unsettled positions every N seconds.

    Runs after the primary settle_once() at 08:00 UTC. Exits early
    when no unsettled positions remain or max cycles are reached.
    """
    interval = settings.settlement_sweep_interval_seconds
    max_cycles = settings.settlement_sweep_max_cycles

    for cycle in range(1, max_cycles + 1):
        await asyncio.sleep(interval)
        remaining = get_expired_unsettled()
        if not remaining:
            logger.info("Sweep cycle %d: no unsettled positions, done", cycle)
            return

        logger.info(
            "Sweep cycle %d/%d: %d unsettled positions, retrying",
            cycle,
            max_cycles,
            len(remaining),
        )
        try:
            await settle_once()
        except Exception:
            logger.exception("Sweep cycle %d failed", cycle)

    logger.warning(
        "ALERT: Sweep exhausted %d cycles. Unsettled positions "
        "may require manual intervention.",
        max_cycles,
    )


async def run():
    """Main loop: settle at 08:00 UTC daily, then sweep for stragglers.

    On startup, runs settle_once() immediately to catch any
    expired positions that were missed (e.g. deploy during
    settlement window). Then enters the daily wait loop.
    After each settlement trigger, sweeps every 5 min for
    remaining unsettled positions.
    """
    logger.info("Expiry settler starting (physical settlement enabled)")

    # Catch-up: settle anything already expired
    try:
        await settle_once()
    except Exception:
        logger.exception("Startup catch-up settlement failed")
    try:
        await _post_settle_sweep()
    except Exception:
        logger.exception("Startup sweep failed")

    while True:
        await _wait_until_target_hour()
        try:
            await settle_once()
        except Exception:
            logger.exception("Expiry settlement failed")
        try:
            await _post_settle_sweep()
        except Exception:
            logger.exception("Post-settlement sweep failed")
