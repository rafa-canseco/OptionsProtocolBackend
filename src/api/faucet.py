"""
Testnet faucet endpoint.

POST /faucet — mints test tokens (LETH + LUSD) to a given address.
Only available when beta_mode is enabled (testnet).
"""
import asyncio
import logging
import re
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from src.config import settings
from src.contracts.abis import MOCK_ERC20_MINT_ABI
from src.contracts.web3_client import get_w3, get_operator_account, build_and_send_tx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Faucet"])

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Mint amounts — keep in sync with the frontend faucet hook
MINT_LUSD = 100_000 * 10**6    # 100,000 LUSD (6 decimals)
MINT_LETH = 50 * 10**18        # 50 LETH (18 decimals)

# Rate limit: 1 request per address per hour
_FAUCET_WINDOW = 3600  # seconds
_FAUCET_MAX_TRACKED = 10_000  # max tracked addresses before triggering stale-entry eviction
_faucet_last_mint: dict[str, float] = {}


def _check_faucet_rate_limit(address: str) -> None:
    """Raise 429 if address already minted within the last hour."""
    now = time.monotonic()
    key = address.lower()

    # Evict stale entries when map grows too large (memory bound)
    if len(_faucet_last_mint) > _FAUCET_MAX_TRACKED:
        stale = [k for k, ts in _faucet_last_mint.items() if now - ts >= _FAUCET_WINDOW]
        for k in stale:
            del _faucet_last_mint[k]

    last = _faucet_last_mint.get(key)
    if last is not None and now - last < _FAUCET_WINDOW:
        remaining = int(_FAUCET_WINDOW - (now - last))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited — try again in {remaining}s",
        )

    _faucet_last_mint[key] = now


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
    leth_amount: str = Field(description="LETH minted (18 decimals, as string)", examples=["50000000000000000000"])
    lusd_amount: str = Field(description="LUSD minted (6 decimals, as string)", examples=["100000000000"])
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
    summary="Mint test tokens (testnet only)",
)
async def faucet(body: FaucetRequest):
    """Mint 50 LETH and 100,000 LUSD to the given address.

    Rate limited to 1 request per address per hour. Only available on testnet
    (Base Sepolia) when beta mode is enabled.

    The operator wallet sends the mint transactions — the recipient does not
    need ETH for gas.
    """
    if not settings.operator_private_key:
        raise HTTPException(503, "Faucet unavailable — operator wallet not configured")

    # Setup infrastructure before consuming rate limit — config errors should
    # not burn the user's hourly allowance
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

    _check_faucet_rate_limit(body.address)

    # Sequential mints to avoid nonce collisions
    try:
        leth_tx = await asyncio.to_thread(
            build_and_send_tx,
            leth_contract.functions.mint(body.address, MINT_LETH),
            account,
        )
    except Exception as exc:
        logger.exception("LETH mint failed for %s", body.address)
        # Roll back rate limit so user can retry
        _faucet_last_mint.pop(body.address.lower(), None)
        raise HTTPException(502, f"LETH mint transaction failed: {type(exc).__name__}")

    try:
        lusd_tx = await asyncio.to_thread(
            build_and_send_tx,
            lusd_contract.functions.mint(body.address, MINT_LUSD),
            account,
        )
    except Exception as exc:
        logger.exception("LUSD mint failed for %s (LETH succeeded: %s)", body.address, leth_tx)
        # Do NOT roll back rate limit — LETH already minted successfully
        raise HTTPException(
            502,
            f"LUSD mint failed ({type(exc).__name__}). "
            f"LETH was minted successfully (tx: {leth_tx}). "
            f"A retry after 1 hour will re-mint both tokens.",
        )

    logger.info("Faucet: minted LETH + LUSD to %s (leth_tx=%s, lusd_tx=%s)", body.address, leth_tx, lusd_tx)

    return FaucetResponse(
        leth_amount=str(MINT_LETH),
        lusd_amount=str(MINT_LUSD),
        leth_tx_hash=leth_tx,
        lusd_tx_hash=lusd_tx,
    )
