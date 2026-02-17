"""
Expiry Settler Bot

Two-phase settlement at 08:00 UTC daily:
  1. batchSettleVaults() — settles all expired vaults (OTM users get collateral back)
  2. physicalRedeem() per ITM position — flash loan + DEX swap delivers contra-asset
"""
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta

from web3 import Web3

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import (
    get_batch_settler,
    get_otoken,
    get_oracle,
    get_uniswap_quoter,
    get_operator_account,
    build_and_send_tx,
)

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 50  # max vaults per tx to avoid gas limit
UNISWAP_FEE_TIER = 3000  # 0.3%


def get_expired_unsettled() -> list[dict]:
    """Get all unsettled positions with expired oTokens."""
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select("user_address, vault_id, otoken_address, expiry, amount, strike_price, is_put")
        .eq("is_settled", False)
        .lt("expiry", now)
        .execute()
    )
    return result.data or []


def identify_itm_positions(positions: list[dict]) -> list[dict]:
    """Separate ITM from OTM positions based on oracle expiry price.

    Reads the oracle's finalized expiry price and compares with strike:
      - PUT is ITM if expiryPrice < strikePrice
      - CALL is ITM if expiryPrice > strikePrice

    Returns only ITM positions (OTM are already handled by batchSettleVaults).
    """
    oracle = get_oracle()
    weth = Web3.to_checksum_address(settings.weth_address)

    itm: list[dict] = []
    # Cache expiry prices to avoid redundant on-chain reads
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

        strike = pos["strike_price"]
        is_put = pos["is_put"]

        # Oracle price and strike are both in 8-decimal format
        is_itm = (is_put and oracle_price < strike) or (not is_put and oracle_price > strike)
        if is_itm:
            pos["expiry_price_raw"] = oracle_price
            itm.append(pos)

    logger.info(f"Identified {len(itm)} ITM out of {len(positions)} expired positions")
    return itm


def compute_max_collateral_spent(position: dict) -> int:
    """Compute maxCollateralSpent for physicalRedeem via Uniswap Quoter.

    1. Determine contra-asset amount (what user receives):
       - PUT ITM: user gets WETH. contra_amount = oTokenAmount * 1e10 (8-dec → 18-dec)
       - CALL ITM: user gets USDC. contra_amount = oTokenAmount * strikePrice / 1e10 (→ 6-dec)
    2. Quote Uniswap: how much collateral needed to produce that exact output
    3. Apply slippage buffer
    """
    amount_raw = int(position["amount"])  # 8 decimals (oToken)
    strike = int(position["strike_price"])  # 8 decimals
    is_put = position["is_put"]

    weth = Web3.to_checksum_address(settings.weth_address)
    usdc = Web3.to_checksum_address(settings.usdc_address)

    if is_put:
        # PUT ITM: collateral is USDC, user receives WETH
        # contra_amount in WETH (18 decimals) = oTokenAmount (8 dec) * 1e10
        contra_amount = amount_raw * (10**10)
        token_in = usdc   # collateral (what gets spent)
        token_out = weth  # contra-asset (what user receives)
    else:
        # CALL ITM: collateral is WETH, user receives USDC
        # contra_amount in USDC (6 decimals) = oTokenAmount (8 dec) * strikePrice (8 dec) / 1e10
        contra_amount = (amount_raw * strike) // (10**10)
        token_in = weth   # collateral (what gets spent)
        token_out = usdc  # contra-asset (what user receives)

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

    max_collateral = math.ceil(amount_in * (1 + settings.swap_slippage_tolerance))
    logger.info(
        f"Swap quote: {amount_in} → max {max_collateral} "
        f"(slippage {settings.swap_slippage_tolerance:.1%}) "
        f"for oToken {position['otoken_address']}"
    )
    return max_collateral


def _mark_settled(
    user_vaults: list[tuple[str, int]],
    settlement_tx: str,
    *,
    settlement_type: str = "cash",
    is_itm: bool = False,
    expiry_price: str | None = None,
) -> None:
    """Mark positions as settled in the DB."""
    client = get_client()
    now = datetime.now(timezone.utc).isoformat()
    for user_addr, vault_id in user_vaults:
        client.table("order_events").update({
            "is_settled": True,
            "settled_at": now,
            "settlement_tx_hash": settlement_tx,
            "settlement_type": settlement_type,
            "is_itm": is_itm,
            "expiry_price": expiry_price,
        }).eq("user_address", user_addr).eq("vault_id", vault_id).execute()


def _mark_physical_delivery(
    user_addr: str,
    vault_id: int,
    delivery_tx: str,
    delivered_asset: str,
    delivered_amount: str,
) -> None:
    """Mark a position with physical delivery details."""
    client = get_client()
    client.table("order_events").update({
        "delivery_tx_hash": delivery_tx,
        "delivered_asset": delivered_asset,
        "delivered_amount": delivered_amount,
    }).eq("user_address", user_addr).eq("vault_id", vault_id).execute()


async def settle_once():
    """Single settlement cycle: 2-phase (batch settle + physical delivery for ITM)."""
    positions = get_expired_unsettled()
    if not positions:
        logger.info("No expired positions to settle")
        return

    # --- Phase 1: batchSettleVaults (settles all vaults, returns excess collateral to OTM) ---
    settler = get_batch_settler()
    account = get_operator_account()

    for i in range(0, len(positions), MAX_BATCH_SIZE):
        batch = positions[i:i + MAX_BATCH_SIZE]
        owners = [p["user_address"] for p in batch]
        vault_ids = [p["vault_id"] for p in batch]

        try:
            tx_fn = settler.functions.batchSettleVaults(owners, vault_ids)
            tx_hash = build_and_send_tx(tx_fn, account)
            logger.info(f"Phase 1: settled {len(batch)} vaults, tx: {tx_hash}")
        except Exception:
            logger.exception(f"Phase 1: batchSettleVaults failed for {len(batch)} vaults")
            return  # don't proceed to phase 2 if settlement fails

    # --- Wait for vaults to be fully settled before physical delivery ---
    delay = settings.flash_loan_redeem_delay_seconds
    logger.info(f"Waiting {delay}s before physical delivery phase")
    await asyncio.sleep(delay)

    # --- Phase 2: physical delivery for ITM positions ---
    itm_positions = await asyncio.to_thread(identify_itm_positions, positions)

    weth = settings.weth_address.lower()
    usdc = settings.usdc_address.lower()

    for pos in itm_positions:
        otoken_addr = pos["otoken_address"]
        user_addr = pos["user_address"]
        amount_raw = int(pos["amount"])
        vault_id = pos["vault_id"]
        expiry_price_str = str(pos.get("expiry_price_raw", ""))

        try:
            max_collateral = await asyncio.to_thread(compute_max_collateral_spent, pos)
        except Exception:
            logger.exception(f"Skipping physical delivery for {otoken_addr} (quote failed)")
            # Mark as cash settlement since physical failed
            _mark_settled(
                [(user_addr, vault_id)], "", settlement_type="cash",
                is_itm=True, expiry_price=expiry_price_str,
            )
            continue

        try:
            tx_fn = settler.functions.physicalRedeem(
                Web3.to_checksum_address(otoken_addr),
                Web3.to_checksum_address(user_addr),
                amount_raw,
                max_collateral,
            )
            tx_hash = build_and_send_tx(tx_fn, account)
            logger.info(f"Phase 2: physical delivery for {user_addr} vault {vault_id}, tx: {tx_hash}")

            # Determine delivered asset
            delivered_asset = weth if pos["is_put"] else usdc
            if pos["is_put"]:
                delivered_amount = str(amount_raw * (10**10))
            else:
                delivered_amount = str((amount_raw * int(pos["strike_price"])) // (10**10))

            _mark_settled(
                [(user_addr, vault_id)], tx_hash,
                settlement_type="physical", is_itm=True,
                expiry_price=expiry_price_str,
            )
            _mark_physical_delivery(
                user_addr, vault_id, tx_hash, delivered_asset, delivered_amount,
            )

        except Exception:
            logger.exception(f"Phase 2: physicalRedeem failed for {otoken_addr} user {user_addr}")
            _mark_settled(
                [(user_addr, vault_id)], "", settlement_type="cash",
                is_itm=True, expiry_price=expiry_price_str,
            )

    # Mark remaining OTM positions as cash-settled
    itm_keys = {(p["user_address"], p["vault_id"]) for p in itm_positions}
    otm_positions = [p for p in positions if (p["user_address"], p["vault_id"]) not in itm_keys]
    if otm_positions:
        for pos in otm_positions:
            expiry_price_str = ""
            expiry = pos["expiry"]
            # Try to get expiry price for OTM too (for display)
            try:
                oracle = get_oracle()
                weth_addr = Web3.to_checksum_address(settings.weth_address)
                price_raw, _ = oracle.functions.getExpiryPrice(weth_addr, expiry).call()
                expiry_price_str = str(price_raw)
            except Exception:
                pass
            _mark_settled(
                [(pos["user_address"], pos["vault_id"])], "",
                settlement_type="cash", is_itm=False,
                expiry_price=expiry_price_str,
            )
        logger.info(f"Marked {len(otm_positions)} OTM positions as cash-settled")


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
