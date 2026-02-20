"""
Demo settlement endpoint for beta mode.

POST /demo/settle — triggers instant settlement for a single vault:
  1. Read vault + oToken details on-chain
  2. Read current ETH price from Chainlink
  3. Set expiry price on Oracle (skip if already set)
  4. batchSettleVaults for the vault
  5. If ITM: physicalRedeem via mock contracts
  6. Update DB + return result
"""
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from web3 import Web3

from src.config import settings
from src.db.database import get_client
from src.pricing.chainlink import get_eth_price
from src.contracts.web3_client import (
    get_oracle,
    get_batch_settler,
    get_controller,
    get_otoken,
    get_operator_account,
    build_and_send_tx,
)
from src.bots.expiry_settler import compute_max_collateral_spent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["demo"])


class SettleRequest(BaseModel):
    user_address: str
    vault_id: int
    otoken_address: str


class SettleResponse(BaseModel):
    settled: bool
    is_itm: bool
    settlement_type: str  # "physical" | "cash"
    expiry_price: str  # 8 decimals, as string
    delivered_asset: str | None = None
    delivered_amount: str | None = None
    settle_tx_hash: str | None = None
    delivery_tx_hash: str | None = None


def _verify_api_key(x_demo_key: str | None) -> None:
    if not settings.demo_api_key:
        raise HTTPException(500, "demo_api_key not configured on server")
    if x_demo_key != settings.demo_api_key:
        raise HTTPException(401, "Invalid or missing X-Demo-Key")


@router.post("/settle", response_model=SettleResponse)
async def demo_settle(
    body: SettleRequest,
    x_demo_key: str | None = Header(None),
):
    _verify_api_key(x_demo_key)

    user = Web3.to_checksum_address(body.user_address)
    vault_id = body.vault_id
    otoken_addr = Web3.to_checksum_address(body.otoken_address)

    # --- Step 1: read vault + oToken details on-chain ---
    controller = get_controller()
    vault = await asyncio.to_thread(
        controller.functions.getVault(user, vault_id).call,
    )
    short_amount = vault[2]  # shortAmount (8 decimals)
    if short_amount == 0:
        raise HTTPException(400, "Vault has no short position (already settled or empty)")

    otoken = get_otoken(otoken_addr)
    strike_price, expiry, is_put = await asyncio.gather(
        asyncio.to_thread(otoken.functions.strikePrice().call),
        asyncio.to_thread(otoken.functions.expiry().call),
        asyncio.to_thread(otoken.functions.isPut().call),
    )

    # --- Step 2: read current ETH price from Chainlink ---
    eth_price_float, _ = await asyncio.to_thread(get_eth_price)
    # Convert to 8-decimal integer (Oracle format)
    oracle_price_8dec = int(eth_price_float * 10**8)
    if oracle_price_8dec == 0:
        raise HTTPException(500, "Chainlink returned zero price")

    # --- Step 3: set expiry price on Oracle (idempotent) ---
    oracle = get_oracle()
    account = get_operator_account()
    weth = Web3.to_checksum_address(settings.weth_address)

    already_set = await asyncio.to_thread(
        oracle.functions.getExpiryPrice(weth, expiry).call,
    )
    if not already_set[1]:  # isFinalized == False → not set yet
        try:
            tx_fn = oracle.functions.setExpiryPrice(weth, expiry, oracle_price_8dec)
            await asyncio.to_thread(build_and_send_tx, tx_fn, account)
            logger.info(f"Set expiry price {oracle_price_8dec} for expiry {expiry}")
        except Exception as e:
            if "PriceAlreadySet" in str(e):
                logger.info(f"Expiry price already set for {expiry}, skipping")
            else:
                logger.exception("Failed to set expiry price")
                raise HTTPException(500, f"Failed to set expiry price: {e}")
    else:
        oracle_price_8dec = already_set[0]
        logger.info(f"Expiry price already finalized for {expiry}: {oracle_price_8dec}")

    # --- Step 4: batchSettleVaults ---
    settler = get_batch_settler()
    try:
        tx_fn = settler.functions.batchSettleVaults([user], [vault_id])
        settle_tx_hash = await asyncio.to_thread(build_and_send_tx, tx_fn, account)
        logger.info(f"Settled vault {vault_id} for {user}, tx: {settle_tx_hash}")
    except Exception as e:
        logger.exception("batchSettleVaults failed")
        raise HTTPException(500, f"Settlement failed: {e}")

    # --- Step 5: determine ITM and physical redeem if needed ---
    is_itm = (is_put and oracle_price_8dec < strike_price) or (
        not is_put and oracle_price_8dec > strike_price
    )

    settlement_type = "cash"
    delivered_asset = None
    delivered_amount = None
    delivery_tx_hash = None

    if is_itm:
        position = {
            "amount": str(short_amount),
            "strike_price": str(strike_price),
            "is_put": is_put,
            "otoken_address": otoken_addr,
        }
        try:
            max_collateral, contra_amount = await asyncio.to_thread(
                compute_max_collateral_spent, position, oracle_price_8dec,
            )
            tx_fn = settler.functions.physicalRedeem(
                otoken_addr, user, short_amount, max_collateral,
            )
            delivery_tx_hash = await asyncio.to_thread(build_and_send_tx, tx_fn, account)
            settlement_type = "physical"
            delivered_asset = settings.weth_address.lower() if is_put else settings.usdc_address.lower()
            delivered_amount = str(contra_amount)
            logger.info(
                f"Physical delivery for vault {vault_id}: "
                f"{delivered_amount} of {delivered_asset}, tx: {delivery_tx_hash}"
            )
        except Exception as e:
            logger.exception("Physical delivery failed")
            settlement_type = "physical_failed"

    # --- Step 6: update DB ---
    now = datetime.now(timezone.utc).isoformat()
    db_fields = {
        "is_settled": True,
        "settled_at": now,
        "settlement_tx_hash": settle_tx_hash,
        "settlement_type": settlement_type,
        "is_itm": is_itm,
        "expiry_price": str(oracle_price_8dec),
    }
    if settlement_type == "physical":
        db_fields["delivered_asset"] = delivered_asset
        db_fields["delivered_amount"] = delivered_amount
        db_fields["delivery_tx_hash"] = delivery_tx_hash

    try:
        client = get_client()
        client.table("order_events").update(db_fields).eq(
            "user_address", body.user_address.lower(),
        ).eq("vault_id", vault_id).execute()
    except Exception:
        logger.exception(
            f"DB update failed after on-chain settlement for vault {vault_id}. "
            f"On-chain state is settled but DB may be stale."
        )

    return SettleResponse(
        settled=True,
        is_itm=is_itm,
        settlement_type=settlement_type,
        expiry_price=str(oracle_price_8dec),
        delivered_asset=delivered_asset,
        delivered_amount=delivered_amount,
        settle_tx_hash=settle_tx_hash,
        delivery_tx_hash=delivery_tx_hash,
    )
