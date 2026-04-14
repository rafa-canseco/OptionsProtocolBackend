"""
XLayer testnet faucet endpoint.

POST /faucet/xlayer — sends gas OKX + mints WOKB + LUSDC to a given address.
Only available when beta_mode is enabled (testnet).
"""

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from src.config import settings
from src.contracts.abis import MOCK_ERC20_MINT_ABI
from src.contracts.web3_client import (
    get_xlayer_w3,
    get_operator_account,
    build_and_send_xlayer_tx,
    build_and_send_xlayer_native_transfer,
)
from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Faucet"])

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Mint amounts
MINT_LUSDC = 100_000 * 10**6  # 100,000 LUSDC (6 decimals)
MINT_WOKB = 50 * 10**18  # 50 WOKB (18 decimals)
MINT_GAS = 10**16  # 0.01 OKX for gas (18 decimals)


def _has_already_claimed(address: str) -> bool:
    client = get_client()
    result = (
        client.table("engagement_events")
        .select("id")
        .eq("event_type", "xlayer_faucet_claim")
        .eq("user_address", address.lower())
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


def _record_claim(address: str, metadata: dict | None = None) -> None:
    client = get_client()
    client.table("engagement_events").insert(
        {
            "user_address": address.lower(),
            "event_type": "xlayer_faucet_claim",
            "metadata": metadata or {},
        }
    ).execute()


class XLayerFaucetRequest(BaseModel):
    address: str = Field(
        description="Ethereum address to receive gas OKX and test tokens",
    )

    @field_validator("address")
    @classmethod
    def validate_eth_address(cls, v: str) -> str:
        if not ETH_ADDRESS_RE.match(v):
            raise ValueError("Invalid Ethereum address")
        return Web3.to_checksum_address(v)


class XLayerFaucetResponse(BaseModel):
    gas_amount: str = Field(description="OKX sent for gas (18 decimals)")
    wokb_amount: str = Field(description="WOKB minted (18 decimals)")
    lusdc_amount: str = Field(description="LUSDC minted (6 decimals)")
    gas_tx_hash: str = Field(description="Tx hash for gas transfer")
    wokb_tx_hash: str = Field(description="Tx hash for WOKB mint")
    lusdc_tx_hash: str = Field(description="Tx hash for LUSDC mint")


@router.post(
    "/faucet/xlayer",
    response_model=XLayerFaucetResponse,
    summary="Mint OKB test tokens + gas (XLayer testnet only)",
)
async def xlayer_faucet(body: XLayerFaucetRequest):
    """Send 0.01 OKX (gas) + 50 WOKB + 100,000 LUSDC to the given address.

    Each wallet can only claim once. Only available on XLayer testnet
    when beta mode is enabled.
    """
    if not settings.operator_private_key:
        raise HTTPException(503, "Faucet unavailable — operator wallet not configured")
    if not settings.xlayer_rpc_url:
        raise HTTPException(503, "Faucet unavailable — XLayer RPC not configured")

    try:
        w3 = get_xlayer_w3()
        account = get_operator_account()
        wokb_contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.wokb_address),
            abi=MOCK_ERC20_MINT_ABI,
        )
        lusdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.xlayer_usdc_address),
            abi=MOCK_ERC20_MINT_ABI,
        )
    except Exception as exc:
        logger.exception("XLayer faucet setup failed")
        raise HTTPException(503, f"Faucet unavailable — {type(exc).__name__}")

    try:
        if _has_already_claimed(body.address):
            raise HTTPException(
                409, "This wallet has already claimed XLayer faucet tokens"
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("XLayer faucet claim check failed for %s", body.address)
        raise HTTPException(
            503, f"Faucet unavailable — database error: {type(exc).__name__}"
        )

    operator_balance = w3.eth.get_balance(account.address)
    min_balance = MINT_GAS + 21_000 * 3 * w3.eth.gas_price
    if operator_balance < min_balance:
        logger.error(
            "XLayer faucet operator balance too low: %s wei (need >= %s)",
            operator_balance,
            min_balance,
        )
        raise HTTPException(
            503, "Faucet temporarily unavailable — operator needs refill"
        )

    # Sequential: gas → WOKB → LUSDC
    try:
        gas_tx = await asyncio.to_thread(
            build_and_send_xlayer_native_transfer,
            body.address,
            MINT_GAS,
            account,
        )
    except Exception as exc:
        logger.exception("XLayer gas transfer failed for %s", body.address)
        raise HTTPException(502, f"Gas transfer failed: {type(exc).__name__}")

    try:
        wokb_tx = await asyncio.to_thread(
            build_and_send_xlayer_tx,
            wokb_contract.functions.mint(body.address, MINT_WOKB),
            account,
        )
    except Exception as exc:
        logger.exception("WOKB mint failed for %s (gas tx: %s)", body.address, gas_tx)
        _record_claim(body.address, {"partial": True, "gas_tx": gas_tx})
        raise HTTPException(
            502,
            f"WOKB mint failed ({type(exc).__name__}). Gas sent: {gas_tx}",
        )

    try:
        lusdc_tx = await asyncio.to_thread(
            build_and_send_xlayer_tx,
            lusdc_contract.functions.mint(body.address, MINT_LUSDC),
            account,
        )
    except Exception as exc:
        logger.exception(
            "LUSDC mint failed for %s (gas=%s, wokb=%s)",
            body.address,
            gas_tx,
            wokb_tx,
        )
        _record_claim(
            body.address,
            {"partial": True, "gas_tx": gas_tx, "wokb_tx": wokb_tx},
        )
        raise HTTPException(
            502,
            f"LUSDC mint failed ({type(exc).__name__}). "
            f"Gas (tx: {gas_tx}) and WOKB (tx: {wokb_tx}) sent.",
        )

    _record_claim(
        body.address,
        {"gas_tx": gas_tx, "wokb_tx": wokb_tx, "lusdc_tx": lusdc_tx},
    )

    logger.info(
        "XLayer faucet: sent to %s (gas=%s, wokb=%s, lusdc=%s)",
        body.address,
        gas_tx,
        wokb_tx,
        lusdc_tx,
    )

    return XLayerFaucetResponse(
        gas_amount=str(MINT_GAS),
        wokb_amount=str(MINT_WOKB),
        lusdc_amount=str(MINT_LUSDC),
        gas_tx_hash=gas_tx,
        wokb_tx_hash=wokb_tx,
        lusdc_tx_hash=lusdc_tx,
    )
