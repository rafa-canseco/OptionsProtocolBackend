"""
Demo settlement endpoint for beta mode.

POST /demo/settle — triggers instant settlement for a single vault:
  1. Read vault + oToken details on-chain
  2. Read current ETH price from Chainlink
  3. Set expiry price on Oracle (reset + re-set if needed for force_itm)
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
from src.pricing.chainlink import get_eth_price_raw
from src.contracts.web3_client import (
    get_oracle,
    get_batch_settler,
    get_controller,
    get_otoken,
    get_operator_account,
    get_w3,
    build_and_send_tx,
)
from src.bots.expiry_settler import compute_slippage_param

MOCK_CHAINLINK_FEED_ABI = [
    {
        "inputs": [{"name": "_price", "type": "int256"}],
        "name": "setPrice",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "price",
        "outputs": [{"name": "", "type": "int256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_APPROVE_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
MAX_UINT256 = 2**256 - 1

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["Demo"])

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_settle_lock = asyncio.Lock()

RPC_SYNC_ATTEMPTS = 10
RPC_SYNC_DELAY = 2  # seconds between polls


async def _wait_for_rpc(read_fn, check_fn, label: str) -> None:
    """Poll an on-chain read until check_fn(result) is True.

    Handles the drpc.live load balancer returning stale data after writes.
    """
    for attempt in range(RPC_SYNC_ATTEMPTS):
        result = await asyncio.to_thread(read_fn)
        if check_fn(result):
            return
        logger.debug(f"RPC sync waiting ({label}): attempt {attempt + 1}/{RPC_SYNC_ATTEMPTS}")
        await asyncio.sleep(RPC_SYNC_DELAY)
    raise HTTPException(500, f"RPC sync timeout: {label}")


class SettleRequest(BaseModel):
    user_address: str = Field(description="Ethereum address of the vault owner", examples=["0xAbC1230000000000000000000000000000000000"])
    vault_id: int = Field(ge=1, description="On-chain vault ID to settle", examples=[1])
    otoken_address: str = Field(description="Address of the oToken contract for this vault", examples=["0xDeF4560000000000000000000000000000000000"])
    force_itm: bool | None = Field(default=None, description="Force settlement outcome: null = real price, true = force ITM, false = force OTM", examples=[None])

    @field_validator("user_address", "otoken_address")
    @classmethod
    def validate_eth_address(cls, v: str) -> str:
        if not ETH_ADDRESS_RE.match(v):
            raise ValueError(f"Invalid Ethereum address: {v}")
        return Web3.to_checksum_address(v)


class SettleResponse(BaseModel):
    settled: bool = Field(description="Whether the vault was successfully settled", examples=[True])
    is_itm: bool = Field(description="Whether the option expired in-the-money", examples=[True])
    settlement_type: Literal["physical", "cash", "physical_failed"] = Field(description="How the position was settled", examples=["physical"])
    expiry_price: str = Field(description="Oracle ETH price at expiry (8 decimals, as string)", examples=["265000000000"])
    settle_tx_hash: str = Field(description="Transaction hash of the batchSettleVaults call", examples=["0xabc123..."])
    delivered_asset: str | None = Field(default=None, description="Address of asset delivered to user (physical settlement only)", examples=["0x4200000000000000000000000000000000000006"])
    delivered_amount: str | None = Field(default=None, description="Amount delivered in native decimals (as string)", examples=["1000000000000000000"])
    delivery_tx_hash: str | None = Field(default=None, description="Transaction hash of the physical delivery", examples=["0xdef456..."])
    warning: str | None = Field(default=None, description="Warning message if settlement partially failed (e.g. physical delivery failed)")


def _verify_api_key(x_demo_key: str | None) -> None:
    if not settings.demo_api_key:
        raise HTTPException(500, "demo_api_key not configured on server")
    if not x_demo_key or not secrets.compare_digest(x_demo_key, settings.demo_api_key):
        raise HTTPException(401, "Invalid or missing X-Demo-Key")


@router.post("/settle", response_model=SettleResponse, summary="Trigger instant settlement (beta)")
async def demo_settle(
    body: SettleRequest,
    x_demo_key: str | None = Header(None, description="API key for demo settlement (required)"),
):
    _verify_api_key(x_demo_key)

    async with _settle_lock:
        return await _do_settle(body)


async def _do_settle(body: SettleRequest) -> SettleResponse:
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

    # Validate that the supplied otoken_address matches the vault's actual shortOtoken
    vault_otoken = Web3.to_checksum_address(vault[0])
    if vault_otoken != otoken_addr:
        raise HTTPException(400, "otoken_address does not match vault's short oToken")

    try:
        already_settled = await asyncio.to_thread(
            controller.functions.vaultSettled(user, vault_id).call,
        )
    except Exception:
        logger.exception(f"Failed to check vaultSettled for user={user} vault={vault_id}")
        raise HTTPException(502, "Cannot verify vault settlement status. Please retry.")
    if already_settled:
        raise HTTPException(400, "Vault is already settled on-chain")

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

    # --- Step 2: determine Oracle expiry price ---
    if body.force_itm is not None:
        # Manipulate price to force ITM or OTM outcome
        # PUT ITM when price < strike, CALL ITM when price > strike
        if body.force_itm:
            # Force ITM: set price 10% below strike for puts, 10% above for calls
            oracle_price_8dec = strike_price * 9 // 10 if is_put else strike_price * 11 // 10
        else:
            # Force OTM: set price 10% above strike for puts, 10% below for calls
            oracle_price_8dec = strike_price * 11 // 10 if is_put else strike_price * 9 // 10
        logger.info(
            f"force_itm={body.force_itm}: using manipulated price {oracle_price_8dec} "
            f"(strike={strike_price}, is_put={is_put})"
        )
    else:
        # Use real Chainlink price
        try:
            raw_answer, decimals, _updated_at = await asyncio.to_thread(get_eth_price_raw)
        except Exception:
            logger.exception("Failed to read ETH price from Chainlink")
            raise HTTPException(500, "Failed to read ETH price from Chainlink")

        if decimals <= 8:
            oracle_price_8dec = raw_answer * (10 ** (8 - decimals))
        else:
            oracle_price_8dec = raw_answer // (10 ** (decimals - 8))

    if oracle_price_8dec <= 0:
        raise HTTPException(500, f"Invalid oracle price: {oracle_price_8dec}")

    # --- Step 3: set expiry price on Oracle (reset + set if price changed) ---
    oracle = get_oracle()
    account = get_operator_account()
    weth = Web3.to_checksum_address(settings.weth_address)

    already_set = await asyncio.to_thread(
        oracle.functions.getExpiryPrice(weth, expiry).call,
    )
    needs_set = not already_set[1]

    if already_set[1] and already_set[0] != oracle_price_8dec:
        logger.warning(
            f"Oracle price already set to {already_set[0]} for expiry {expiry}, "
            f"wanted {oracle_price_8dec}. Price cannot be changed after being set."
        )

    if needs_set:
        try:
            tx_fn = oracle.functions.setExpiryPrice(weth, expiry, oracle_price_8dec)
            await asyncio.to_thread(build_and_send_tx, tx_fn, account)
            logger.info(f"Set expiry price {oracle_price_8dec} for expiry {expiry}")
        except Exception as e:
            if "PriceAlreadySet" in str(e):
                logger.info(f"Expiry price already set for {expiry}, reading on-chain value")
            else:
                logger.exception("Failed to set expiry price")
                raise HTTPException(500, "Failed to set expiry price on Oracle")

        # Wait for price to be visible
        await _wait_for_rpc(
            oracle.functions.getExpiryPrice(weth, expiry).call,
            lambda r: r[1] and r[0] > 0,  # isFinalized and price > 0
            "Oracle price propagation",
        )
        confirmed = await asyncio.to_thread(oracle.functions.getExpiryPrice(weth, expiry).call)
        oracle_price_8dec = confirmed[0]
    else:
        # Price already finalized and matches (or no force_itm)
        oracle_price_8dec = already_set[0]
        logger.info(f"Expiry price already finalized for {expiry}: {oracle_price_8dec}")

    # --- Step 3b: sync MockChainlinkFeed so MockSwapRouter uses the same price ---
    if settings.mock_chainlink_feed_address:
        try:
            w3 = get_w3()
            mock_feed = w3.eth.contract(
                address=Web3.to_checksum_address(settings.mock_chainlink_feed_address),
                abi=MOCK_CHAINLINK_FEED_ABI,
            )
            tx_fn = mock_feed.functions.setPrice(oracle_price_8dec)
            await asyncio.to_thread(build_and_send_tx, tx_fn, account)

            await _wait_for_rpc(
                mock_feed.functions.price().call,
                lambda p: p == oracle_price_8dec,
                "MockChainlinkFeed price propagation",
            )
            logger.info(f"Synced MockChainlinkFeed price to {oracle_price_8dec}")
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to sync MockChainlinkFeed price")
            raise HTTPException(500, "Failed to sync swap router price feed")

    # --- Step 4: batchSettleVaults ---
    settler = get_batch_settler()
    try:
        tx_fn = settler.functions.batchSettleVaults([user], [vault_id])
        settle_tx_hash = await asyncio.to_thread(build_and_send_tx, tx_fn, account)
        logger.info(f"Settled vault {vault_id} for {user}, tx: {settle_tx_hash}")

        await _wait_for_rpc(
            controller.functions.vaultSettled(user, vault_id).call,
            lambda settled: settled is True,
            "batchSettleVaults propagation",
        )
    except HTTPException:
        raise
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
            # Approve oToken to BatchSettler if needed (operator must allow pull)
            w3 = get_w3()
            otoken_erc20 = w3.eth.contract(
                address=otoken_addr, abi=ERC20_APPROVE_ABI,
            )
            settler_addr = Web3.to_checksum_address(settings.batch_settler_address)
            allowance = await asyncio.to_thread(
                otoken_erc20.functions.allowance(account.address, settler_addr).call,
            )
            if allowance < short_amount:
                approve_fn = otoken_erc20.functions.approve(settler_addr, MAX_UINT256)
                await asyncio.to_thread(build_and_send_tx, approve_fn, account)

                await _wait_for_rpc(
                    otoken_erc20.functions.allowance(account.address, settler_addr).call,
                    lambda a: a >= short_amount,
                    "oToken approve propagation",
                )
                logger.info(f"Approved oToken {otoken_addr} to BatchSettler")

            slippage_param, contra_amount = await asyncio.to_thread(
                compute_slippage_param, position, oracle_price_8dec,
            )
            tx_fn = settler.functions.physicalRedeem(
                otoken_addr, user, short_amount, slippage_param,
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
