"""
Demo settlement endpoint for beta mode.

POST /demo/settle — triggers instant settlement for a single vault:
  1. Read vault + oToken details on-chain
  2. Read current ETH price from Chainlink
  3. Set expiry price on Oracle (skip if already finalized)
  4. batchSettleVaults for the vault
  5. Determine ITM status; if ITM, physicalRedeem via BatchSettler
  6. Update DB + return result
"""
import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
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

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class SettleRequest(BaseModel):
    user_address: str
    vault_id: int = Field(ge=1)
    otoken_address: str

    @field_validator("user_address", "otoken_address")
    @classmethod
    def validate_eth_address(cls, v: str) -> str:
        if not ETH_ADDRESS_RE.match(v):
            raise ValueError(f"Invalid Ethereum address: {v}")
        return Web3.to_checksum_address(v)


class SettleResponse(BaseModel):
    settled: bool
    is_itm: bool
    settlement_type: Literal["physical", "cash", "physical_failed"]
    expiry_price: str  # 8 decimals, as string
    settle_tx_hash: str
    delivered_asset: str | None = None
    delivered_amount: str | None = None
    delivery_tx_hash: str | None = None
    warning: str | None = None


def _verify_api_key(x_demo_key: str | None) -> None:
    if not settings.demo_api_key:
        raise HTTPException(500, "demo_api_key not configured on server")
    if not x_demo_key or not secrets.compare_digest(x_demo_key, settings.demo_api_key):
        raise HTTPException(401, "Invalid or missing X-Demo-Key")


@router.post("/settle", response_model=SettleResponse)
async def demo_settle(
    body: SettleRequest,
    x_demo_key: str | None = Header(None),
):
    _verify_api_key(x_demo_key)

    user = body.user_address  # already checksummed by validator
    vault_id = body.vault_id
    otoken_addr = body.otoken_address  # already checksummed by validator

    # --- Step 1: read vault + oToken details on-chain ---
    try:
        controller = get_controller()
        # getVault returns (shortOtoken, collateralAsset, shortAmount, collateralAmount)
        vault = await asyncio.to_thread(
            controller.functions.getVault(user, vault_id).call,
        )
        short_amount = vault[2]  # shortAmount (8 decimals)
    except Exception:
        logger.exception(f"Failed to read vault {vault_id} for {user}")
        raise HTTPException(500, "Failed to read vault from chain")

    if short_amount == 0:
        raise HTTPException(400, "Vault has no short position (already settled or empty)")

    try:
        otoken = get_otoken(otoken_addr)
        strike_price, expiry, is_put = await asyncio.gather(
            asyncio.to_thread(otoken.functions.strikePrice().call),
            asyncio.to_thread(otoken.functions.expiry().call),
            asyncio.to_thread(otoken.functions.isPut().call),
        )
    except Exception:
        logger.exception(f"Failed to read oToken details for {otoken_addr}")
        raise HTTPException(500, "Failed to read oToken details from chain")

    # --- Step 2: read current ETH price from Chainlink ---
    try:
        eth_price_float, _updated_at = await asyncio.to_thread(get_eth_price)
    except Exception:
        logger.exception("Failed to read ETH price from Chainlink")
        raise HTTPException(500, "Failed to read ETH price from Chainlink")

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
    if not already_set[1]:  # isFinalized == False → not yet finalized, safe to set
        try:
            tx_fn = oracle.functions.setExpiryPrice(weth, expiry, oracle_price_8dec)
            await asyncio.to_thread(build_and_send_tx, tx_fn, account)
            logger.info(f"Set expiry price {oracle_price_8dec} for expiry {expiry}")
        except Exception as e:
            if "PriceAlreadySet" in str(e):
                logger.info(f"Expiry price already set for {expiry}, reading on-chain value")
                on_chain = await asyncio.to_thread(
                    oracle.functions.getExpiryPrice(weth, expiry).call,
                )
                oracle_price_8dec = on_chain[0]
            else:
                logger.exception("Failed to set expiry price")
                raise HTTPException(500, "Failed to set expiry price on Oracle")
    else:
        oracle_price_8dec = already_set[0]
        logger.info(f"Expiry price already finalized for {expiry}: {oracle_price_8dec}")

    # --- Step 4: batchSettleVaults ---
    settler = get_batch_settler()
    try:
        tx_fn = settler.functions.batchSettleVaults([user], [vault_id])
        settle_tx_hash = await asyncio.to_thread(build_and_send_tx, tx_fn, account)
        logger.info(f"Settled vault {vault_id} for {user}, tx: {settle_tx_hash}")
    except Exception:
        logger.exception("batchSettleVaults failed")
        raise HTTPException(500, "Vault settlement failed")

    # --- Step 5: determine ITM and physical redeem if needed ---
    is_itm = (is_put and oracle_price_8dec < strike_price) or (
        not is_put and oracle_price_8dec > strike_price
    )

    settlement_type: Literal["physical", "cash", "physical_failed"] = "cash"
    delivered_asset = None
    delivered_amount = None
    delivery_tx_hash = None
    warning = None

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
        except Exception:
            logger.exception(
                f"Physical delivery failed for vault {vault_id}, user {user}. "
                f"Vault is settled on-chain but ITM delivery did not complete."
            )
            settlement_type = "physical_failed"
            warning = (
                "Vault settled on-chain but physical delivery of ITM asset failed. "
                f"Settlement tx: {settle_tx_hash}. Contact support for manual delivery."
            )

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

    db_warning = None
    try:
        client = get_client()
        result = client.table("order_events").update(db_fields).eq(
            "user_address", body.user_address.lower(),
        ).eq("vault_id", vault_id).execute()
        if not result.data:
            logger.warning(
                f"DB update matched no rows for user={body.user_address.lower()} "
                f"vault={vault_id}. On-chain settlement succeeded but DB not updated."
            )
            db_warning = "Position settled on-chain but not yet indexed in DB. UI may take a moment to update."
    except Exception:
        logger.exception(
            f"DB update failed after on-chain settlement for vault {vault_id}. "
            f"On-chain state is settled but DB may be stale."
        )
        db_warning = "Position settled on-chain but DB update failed. UI may take a moment to update."

    # Combine warnings if both physical delivery and DB had issues
    if warning and db_warning:
        warning = f"{warning} Also: {db_warning}"
    elif db_warning:
        warning = db_warning

    return SettleResponse(
        settled=True,
        is_itm=is_itm,
        settlement_type=settlement_type,
        expiry_price=str(oracle_price_8dec),
        delivered_asset=delivered_asset,
        delivered_amount=delivered_amount,
        settle_tx_hash=settle_tx_hash,
        delivery_tx_hash=delivery_tx_hash,
        warning=warning,
    )
