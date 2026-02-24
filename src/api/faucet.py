"""
Testnet faucet endpoint.

POST /faucet — mints test tokens (LETH + LUSD) and sends gas ETH to a given address.
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
from src.contracts.web3_client import get_w3, get_operator_account, build_and_send_tx, build_and_send_eth_transfer

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Faucet"])

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Mint amounts — keep in sync with the frontend faucet hook
MINT_LUSD = 100_000 * 10**6    # 100,000 LUSD (6 decimals)
MINT_LETH = 50 * 10**18        # 50 LETH (18 decimals)
MINT_ETH = 5 * 10**15          # 0.005 ETH for gas (18 decimals)

# 1 claim per wallet (permanent, not time-based)
_claimed_wallets: set[str] = set()


def _check_already_claimed(address: str) -> None:
    """Raise 409 if this wallet has already claimed."""
    if address.lower() in _claimed_wallets:
        raise HTTPException(
            status_code=409,
            detail="This wallet has already claimed faucet tokens",
        )


class FaucetRequest(BaseModel):
    address: str = Field(
        description="Ethereum address to receive test tokens",
        examples=["0xAbC1230000000000000000000000000000000000"],
    )

    @field_validator("address")
    @classmethod
    def validate_eth_address(cls, v: str) -> str:
        if not ETH_ADDRESS_RE.match(v):
            raise ValueError("Invalid Ethereum address")
        return Web3.to_checksum_address(v)


class FaucetResponse(BaseModel):
    eth_amount: str = Field(description="ETH sent for gas (18 decimals, as string)", examples=["5000000000000000"])
    leth_amount: str = Field(description="LETH minted (18 decimals, as string)", examples=["50000000000000000000"])
    lusd_amount: str = Field(description="LUSD minted (6 decimals, as string)", examples=["100000000000"])
    eth_tx_hash: str = Field(
        description="Transaction hash for ETH gas transfer",
        examples=["0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"],
    )
    leth_tx_hash: str = Field(
        description="Transaction hash for LETH mint",
        examples=["0xa1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"],
    )
    lusd_tx_hash: str = Field(
        description="Transaction hash for LUSD mint",
        examples=["0xf6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5"],
    )


@router.post(
    "/faucet",
    response_model=FaucetResponse,
    summary="Mint test tokens and gas ETH (testnet only)",
)
async def faucet(body: FaucetRequest):
    """Send 0.005 ETH (gas) + 50 LETH + 100,000 LUSD to the given address.

    Each wallet can only claim once. Only available on testnet
    (Base Sepolia) when beta mode is enabled.

    The operator wallet sends all transactions — the recipient does not
    need existing ETH for gas.
    """
    if not settings.operator_private_key:
        raise HTTPException(503, "Faucet unavailable — operator wallet not configured")

    # Setup infrastructure before checking claim status — config errors should
    # not produce confusing "already claimed" on retry after a config fix
    try:
        w3 = get_w3()
        account = get_operator_account()
        leth_contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.weth_address),
            abi=MOCK_ERC20_MINT_ABI,
        )
        lusd_contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.usdc_address),
            abi=MOCK_ERC20_MINT_ABI,
        )
    except Exception as exc:
        logger.exception("Faucet infrastructure setup failed")
        raise HTTPException(503, f"Faucet unavailable — configuration error: {type(exc).__name__}")

    _check_already_claimed(body.address)

    # Sequential sends to avoid nonce collisions: ETH → LETH → LUSD
    try:
        eth_tx = await asyncio.to_thread(
            build_and_send_eth_transfer,
            body.address,
            MINT_ETH,
            account,
        )
    except Exception as exc:
        logger.exception("ETH gas transfer failed for %s", body.address)
        raise HTTPException(502, f"ETH gas transfer failed: {type(exc).__name__}")

    try:
        leth_tx = await asyncio.to_thread(
            build_and_send_tx,
            leth_contract.functions.mint(body.address, MINT_LETH),
            account,
        )
    except Exception as exc:
        logger.exception("LETH mint failed for %s (ETH succeeded: %s)", body.address, eth_tx)
        raise HTTPException(
            502,
            f"LETH mint failed ({type(exc).__name__}). "
            f"ETH gas was sent successfully (tx: {eth_tx}).",
        )

    try:
        lusd_tx = await asyncio.to_thread(
            build_and_send_tx,
            lusd_contract.functions.mint(body.address, MINT_LUSD),
            account,
        )
    except Exception as exc:
        logger.exception("LUSD mint failed for %s (ETH=%s, LETH=%s)", body.address, eth_tx, leth_tx)
        # Mark as claimed — ETH + LETH already sent, partial state is not retryable
        _claimed_wallets.add(body.address.lower())
        raise HTTPException(
            502,
            f"LUSD mint failed ({type(exc).__name__}). "
            f"ETH gas (tx: {eth_tx}) and LETH (tx: {leth_tx}) were sent successfully.",
        )

    # All 3 transactions succeeded — mark wallet as claimed
    _claimed_wallets.add(body.address.lower())

    logger.info(
        "Faucet: sent ETH + LETH + LUSD to %s (eth_tx=%s, leth_tx=%s, lusd_tx=%s)",
        body.address, eth_tx, leth_tx, lusd_tx,
    )

    return FaucetResponse(
        eth_amount=str(MINT_ETH),
        leth_amount=str(MINT_LETH),
        lusd_amount=str(MINT_LUSD),
        eth_tx_hash=eth_tx,
        leth_tx_hash=leth_tx,
        lusd_tx_hash=lusd_tx,
    )
