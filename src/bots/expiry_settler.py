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
    get_oracle,
    get_uniswap_quoter,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 50  # max vaults per tx to avoid gas limit
UNISWAP_FEE_TIER = 3000  # 0.3% — standard tier for ETH/USDC on Uniswap V3


def get_expired_unsettled() -> list[dict]:
    """Get all unsettled positions with expired oTokens.

    Filters out rows missing settlement-critical fields (strike_price, is_put)
    which can happen if oToken metadata enrichment failed during indexing.
    """
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select("user_address, vault_id, otoken_address, expiry, amount, strike_price, is_put")
        .eq("is_settled", False)
        .lt("expiry", now)
        .not_.is_("strike_price", "null")
        .not_.is_("is_put", "null")
        .not_.is_("amount", "null")
        .execute()
    )
    return result.data or []


def identify_itm_positions(
    positions: list[dict],
) -> tuple[list[dict], dict[int, int | None]]:
    """Separate ITM from OTM positions based on oracle expiry price.

    Reads the oracle's finalized expiry price and compares with strike:
      - PUT is ITM if expiryPrice < strikePrice
      - CALL is ITM if expiryPrice > strikePrice

    Returns (itm_positions, expiry_price_cache) so the cache can be reused
    for OTM marking without redundant on-chain reads.
    """
    oracle = get_oracle()
    weth = Web3.to_checksum_address(settings.weth_address)

    itm: list[dict] = []
    expiry_price_cache: dict[int, int | None] = {}

    for pos in positions:
        expiry = pos["expiry"]
        if expiry not in expiry_price_cache:
            try:
                price_raw, is_finalized = oracle.functions.getExpiryPrice(weth, expiry).call()
                expiry_price_cache[expiry] = price_raw if is_finalized else None
            except Exception:
                logger.exception(f"Failed to read expiry price for timestamp {expiry}")
                expiry_price_cache[expiry] = None

        oracle_price = expiry_price_cache[expiry]
        if oracle_price is None:
            logger.warning(f"Expiry price not finalized for {expiry}, skipping position")
            continue

        strike = int(pos["strike_price"])
        is_put = pos["is_put"]

        is_itm = (is_put and oracle_price < strike) or (not is_put and oracle_price > strike)
        if is_itm:
            pos["expiry_price_raw"] = oracle_price
            itm.append(pos)

    logger.info(f"Identified {len(itm)} ITM out of {len(positions)} expired positions")
    return itm, expiry_price_cache


def compute_max_collateral_spent(position: dict) -> tuple[int, int]:
    """Compute maxCollateralSpent for physicalRedeem via Uniswap Quoter.

    Returns (max_collateral_spent, contra_amount) so the caller can reuse
    contra_amount as delivered_amount without recalculating.

    1. Determine contra-asset amount (what user receives):
       - PUT ITM: user gets WETH. contra_amount = oTokenAmount * 1e10
         decimal math: 10^8 * 10^10 = 10^18 (WETH 18-dec)
       - CALL ITM: user gets USDC. contra_amount = oTokenAmount * strikePrice / 1e10
         decimal math: 10^8 * 10^8 / 10^10 = 10^6 (USDC 6-dec)
    2. Quote Uniswap: how much collateral needed to produce that exact output
    3. Apply slippage buffer (integer arithmetic to avoid float precision loss)
    """
    amount_raw = int(position["amount"])  # 8 decimals (oToken)
    strike = int(position["strike_price"])  # 8 decimals
    is_put = position["is_put"]

    weth = Web3.to_checksum_address(settings.weth_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    if is_put:
        contra_amount = amount_raw * (10**10)
        token_in = usdc
        token_out = weth
    else:
        contra_amount = (amount_raw * strike) // (10**10)
        token_in = weth
        token_out = usdc

    quoter = get_uniswap_quoter()
    try:
        result = quoter.functions.quoteExactOutputSingle(
            (token_in, token_out, contra_amount, UNISWAP_FEE_TIER, 0)
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
        f"Swap quote: {amount_in} → max {max_collateral} "
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
            .eq("user_address", user_addr)
            .eq("vault_id", vault_id)
            .execute()
        )
        if not result.data:
            logger.error(f"{context}: matched no rows user={user_addr} vault={vault_id}")
    except Exception:
        logger.exception(f"{context}: DB write failed user={user_addr} vault={vault_id}")
        raise


async def settle_once():
    """Single settlement cycle: 2-phase (batch settle + physical delivery for ITM)."""
    positions = get_expired_unsettled()
    if not positions:
        logger.info("No expired positions to settle")
        return

    # --- Phase 1: batchSettleVaults (settles all expired vaults on-chain) ---
    settler = get_batch_settler()
    account = get_operator_account()

    settled_positions: list[dict] = []
    phase1_tx_map: dict[tuple[str, int], str] = {}  # (user, vault_id) → tx_hash
    phase1_failed = False

    for i in range(0, len(positions), MAX_BATCH_SIZE):
        batch = positions[i:i + MAX_BATCH_SIZE]
        owners = [p["user_address"] for p in batch]
        vault_ids = [p["vault_id"] for p in batch]

        # Step 1: on-chain settlement
        try:
            tx_fn = settler.functions.batchSettleVaults(owners, vault_ids)
            tx_hash = build_and_send_tx(tx_fn, account)
            logger.info(f"Phase 1: settled {len(batch)} vaults on-chain, tx: {tx_hash}")
        except Exception:
            logger.exception(f"Phase 1: batchSettleVaults tx failed for {len(batch)} vaults")
            phase1_failed = True
            break

        # On-chain succeeded — these vaults ARE settled regardless of DB outcome
        settled_positions.extend(batch)
        for o, v in zip(owners, vault_ids):
            phase1_tx_map[(o, v)] = tx_hash

        # Step 2: mark in DB (separate try so on-chain success is never misattributed)
        now = datetime.now(timezone.utc).isoformat()
        try:
            _mark_batch_settled(owners, vault_ids, tx_hash, now)
        except Exception:
            logger.exception(
                f"Phase 1: DB write failed after on-chain success (tx: {tx_hash}). "
                f"{len(batch)} vaults settled on-chain but not marked in DB."
            )
            # Continue — vaults are in settled_positions so Phase 2 can still run

    if not settled_positions:
        logger.error("Phase 1: no batches settled successfully, aborting")
        return

    if phase1_failed:
        logger.warning(
            f"Phase 1: partial success — {len(settled_positions)}/{len(positions)} "
            f"vaults settled. Continuing with settled vaults only."
        )

    # --- Wait for vaults to be fully settled before physical delivery ---
    delay = settings.flash_loan_redeem_delay_seconds
    logger.info(f"Waiting {delay}s before physical delivery phase")
    await asyncio.sleep(delay)

    # --- Phase 2: physical delivery for ITM positions ---
    itm_positions, expiry_cache = await asyncio.to_thread(
        identify_itm_positions, settled_positions,
    )

    weth = settings.weth_address.lower()
    usdc = settings.usdc_address.lower()

    for pos in itm_positions:
        otoken_addr = pos["otoken_address"]
        user_addr = pos["user_address"]
        amount_raw = int(pos["amount"])
        vault_id = pos["vault_id"]
        expiry_price_raw = pos.get("expiry_price_raw")
        expiry_price_str = str(expiry_price_raw) if expiry_price_raw is not None else None

        # Step 1: get swap quote
        try:
            max_collateral, contra_amount = await asyncio.to_thread(
                compute_max_collateral_spent, pos,
            )
        except Exception:
            logger.exception(
                f"ALERT: Skipping physical delivery for {otoken_addr} user={user_addr} "
                f"(quote failed). Position requires manual review."
            )
            try:
                _db_update(user_addr, vault_id, {
                    "settlement_type": "physical_failed",
                    "is_itm": True,
                    "expiry_price": expiry_price_str,
                }, "Phase 2 quote-fail mark")
            except Exception:
                pass  # already logged by _db_update
            continue

        # Step 2: on-chain physical delivery
        delivery_succeeded = False
        tx_hash = None
        try:
            tx_fn = settler.functions.physicalRedeem(
                Web3.to_checksum_address(otoken_addr),
                Web3.to_checksum_address(user_addr),
                amount_raw,
                max_collateral,
            )
            tx_hash = build_and_send_tx(tx_fn, account)
            delivery_succeeded = True
            logger.info(
                f"Phase 2: physical delivery for {user_addr} vault {vault_id}, tx: {tx_hash}"
            )
        except Exception:
            logger.exception(
                f"ALERT: physicalRedeem tx failed for {otoken_addr} user={user_addr} "
                f"vault={vault_id}. Position requires manual review."
            )

        # Step 3: mark DB (separate from on-chain to prevent misattribution)
        if delivery_succeeded:
            delivered_asset = weth if pos["is_put"] else usdc
            delivered_amount = str(contra_amount)
            try:
                _db_update(user_addr, vault_id, {
                    "settlement_type": "physical",
                    "is_itm": True,
                    "expiry_price": expiry_price_str,
                    "delivery_tx_hash": tx_hash,
                    "delivered_asset": delivered_asset,
                    "delivered_amount": delivered_amount,
                }, "Phase 2 delivery mark")
            except Exception:
                logger.error(
                    f"ALERT: Physical delivery succeeded on-chain (tx: {tx_hash}) but "
                    f"DB write failed for user={user_addr} vault={vault_id}. "
                    f"DB shows 'cash' but user received physical delivery."
                )
        else:
            try:
                _db_update(user_addr, vault_id, {
                    "settlement_type": "physical_failed",
                    "is_itm": True,
                    "expiry_price": expiry_price_str,
                }, "Phase 2 delivery-fail mark")
            except Exception:
                pass  # already logged by _db_update

    # Update OTM positions with expiry price and ITM flag (display only)
    itm_keys = {(p["user_address"], p["vault_id"]) for p in itm_positions}
    otm_positions = [
        p for p in settled_positions
        if (p["user_address"], p["vault_id"]) not in itm_keys
    ]
    if otm_positions:
        for pos in otm_positions:
            expiry = pos["expiry"]
            cached_price = expiry_cache.get(expiry)
            expiry_price_str = str(cached_price) if cached_price is not None else None
            try:
                _db_update(pos["user_address"], pos["vault_id"], {
                    "is_itm": False,
                    "expiry_price": expiry_price_str,
                }, "OTM expiry price update")
            except Exception:
                pass  # already logged by _db_update
        logger.info(f"Updated {len(otm_positions)} OTM positions with expiry data")


def _mark_batch_settled(
    owners: list[str], vault_ids: list[int], tx_hash: str, now: str,
) -> None:
    """Mark a batch of positions as settled in the DB. Raises on first failure."""
    client = get_client()
    for user_addr, vault_id in zip(owners, vault_ids):
        result = client.table("order_events").update({
            "is_settled": True,
            "settled_at": now,
            "settlement_tx_hash": tx_hash,
            "settlement_type": "cash",
        }).eq("user_address", user_addr).eq("vault_id", vault_id).execute()
        if not result.data:
            logger.error(
                f"_mark_batch_settled matched no rows: user={user_addr} vault={vault_id}"
            )


async def _wait_until_target_hour():
    """Sleep until the next 08:00 UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(
        hour=settings.expiry_settle_hour_utc,
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)

    wait_seconds = (target - now).total_seconds()
    logger.info(f"Expiry settler waiting {wait_seconds:.0f}s until {target.isoformat()}")
    await asyncio.sleep(wait_seconds)


async def run():
    """Main loop: settle at 08:00 UTC daily."""
    logger.info("Expiry settler starting (physical settlement enabled)")
    while True:
        await _wait_until_target_hour()
        try:
            await settle_once()
        except Exception:
            logger.exception("Expiry settlement failed")
