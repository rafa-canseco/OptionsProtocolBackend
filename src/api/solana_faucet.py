"""
Solana devnet faucet endpoint.

POST /faucet/solana — sends SOL for gas/rent and transfers USDC
to a given Solana wallet. Only available when beta_mode is enabled
and Solana is configured.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.message import MessageV0  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.system_program import (  # type: ignore[import-untyped]
    TransferParams,
    transfer,
)
from solders.transaction import (  # type: ignore[import-untyped]
    VersionedTransaction,
)
from spl.token.constants import (  # type: ignore[import-untyped]
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from spl.token.instructions import (  # type: ignore[import-untyped]
    TransferCheckedParams,
    create_associated_token_account,
    transfer_checked,
)

from src.chains.address import is_valid_solana_address
from src.chains.solana.client import (
    get_balance,
    get_sol_balance,
    get_solana_client,
    get_solana_operator,
)
from src.config import has_solana_config, settings
from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Faucet"])

# Faucet amounts
FAUCET_SOL = 100_000_000  # 0.1 SOL in lamports
FAUCET_USDC = 10_000 * 10**6  # 10,000 USDC (6 decimals)
USDC_DECIMALS = 6


def _derive_ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the Associated Token Address for owner + mint."""
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def _has_already_claimed(address: str) -> bool:
    """Check Supabase for an existing solana_faucet_claim for this wallet."""
    client = get_client()
    result = (
        client.table("engagement_events")
        .select("id")
        .eq("event_type", "solana_faucet_claim")
        .eq("user_address", address)
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


def _record_claim(address: str, metadata: dict | None = None) -> None:
    """Insert a solana_faucet_claim event into Supabase."""
    client = get_client()
    client.table("engagement_events").insert(
        {
            "user_address": address,
            "event_type": "solana_faucet_claim",
            "metadata": metadata or {},
        }
    ).execute()


def _send_sol(operator: Keypair, recipient: Pubkey, lamports: int) -> str:
    """Build and send a SOL transfer transaction. Returns tx signature."""
    rpc = get_solana_client()
    ix = transfer(
        TransferParams(
            from_pubkey=operator.pubkey(),
            to_pubkey=recipient,
            lamports=lamports,
        )
    )
    blockhash_resp = rpc.get_latest_blockhash()
    blockhash = blockhash_resp.value.blockhash
    msg = MessageV0.try_compile(
        payer=operator.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [operator])
    resp = rpc.send_transaction(tx)
    sig_obj = resp.value
    rpc.confirm_transaction(sig_obj, sleep_seconds=0.5)
    sig = str(sig_obj)
    logger.info("SOL transfer confirmed: %s", sig)
    return sig


def _send_usdc(
    operator: Keypair,
    recipient: Pubkey,
    mint: Pubkey,
    amount: int,
) -> str:
    """Transfer USDC from operator's ATA to recipient's ATA.

    Creates the recipient's ATA if it doesn't exist (operator pays rent).
    Returns tx signature.
    """
    rpc = get_solana_client()
    operator_ata = _derive_ata(operator.pubkey(), mint)
    recipient_ata = _derive_ata(recipient, mint)

    instructions = []

    # Create recipient ATA if it doesn't exist
    ata_info = rpc.get_account_info(recipient_ata)
    if ata_info.value is None:
        instructions.append(
            create_associated_token_account(
                payer=operator.pubkey(),
                owner=recipient,
                mint=mint,
            )
        )

    instructions.append(
        transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=operator_ata,
                mint=mint,
                dest=recipient_ata,
                owner=operator.pubkey(),
                amount=amount,
                decimals=USDC_DECIMALS,
            )
        )
    )

    blockhash_resp = rpc.get_latest_blockhash()
    blockhash = blockhash_resp.value.blockhash
    msg = MessageV0.try_compile(
        payer=operator.pubkey(),
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [operator])
    resp = rpc.send_transaction(tx)
    sig_obj = resp.value
    rpc.confirm_transaction(sig_obj, sleep_seconds=0.5)
    sig = str(sig_obj)
    logger.info("USDC transfer confirmed: %s", sig)
    return sig


class SolanaFaucetRequest(BaseModel):
    address: str = Field(
        description="Solana wallet address (base58 pubkey)",
        examples=["7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"],
    )

    @field_validator("address")
    @classmethod
    def validate_solana_address(cls, v: str) -> str:
        if not is_valid_solana_address(v):
            raise ValueError("Invalid Solana address")
        return v


class SolanaFaucetResponse(BaseModel):
    sol_amount: str = Field(description="SOL sent in lamports", examples=["100000000"])
    usdc_amount: str = Field(
        description="USDC sent (6 decimals)", examples=["10000000000"]
    )
    sol_tx_signature: str = Field(description="Transaction signature for SOL transfer")
    usdc_tx_signature: str = Field(
        description="Transaction signature for USDC transfer"
    )


@router.post(
    "/faucet/solana",
    response_model=SolanaFaucetResponse,
    summary="Send SOL + USDC on Solana devnet (testnet only)",
)
async def solana_faucet(body: SolanaFaucetRequest):
    """Send 0.1 SOL (gas/rent) + 10,000 USDC to a Solana wallet.

    Each wallet can only claim once (persisted in Supabase). Only available
    on devnet when beta mode is enabled and Solana is configured.
    """
    if not has_solana_config():
        raise HTTPException(503, "Solana faucet unavailable — not configured")
    if not settings.solana_usdc_mint:
        raise HTTPException(503, "Solana faucet unavailable — USDC mint not set")

    try:
        operator = get_solana_operator()
        usdc_mint = Pubkey.from_string(settings.solana_usdc_mint)
    except Exception as exc:
        logger.exception("Solana faucet setup failed")
        raise HTTPException(
            503,
            f"Solana faucet unavailable — config error: {type(exc).__name__}",
        )

    # Check duplicate claim
    try:
        if _has_already_claimed(body.address):
            raise HTTPException(
                409, "This wallet has already claimed Solana faucet tokens"
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Faucet claim check failed for %s", body.address)
        raise HTTPException(
            503, f"Faucet unavailable — database error: {type(exc).__name__}"
        )

    # Pre-flight: check operator balances
    operator_pub = str(operator.pubkey())
    sol_balance = get_sol_balance(operator_pub)
    min_sol = FAUCET_SOL + 10_000_000  # 0.01 SOL buffer for rent + fees
    if sol_balance < min_sol:
        logger.error(
            "Solana faucet operator SOL too low: %d lamports (need %d). Pubkey: %s",
            sol_balance,
            min_sol,
            operator_pub,
        )
        raise HTTPException(
            503, "Faucet temporarily unavailable — operator needs SOL refill"
        )

    usdc_balance = get_balance(operator_pub, settings.solana_usdc_mint)
    if usdc_balance < FAUCET_USDC:
        logger.error(
            "Solana faucet operator USDC too low: %d (need %d). Pubkey: %s",
            usdc_balance,
            FAUCET_USDC,
            operator_pub,
        )
        raise HTTPException(
            503, "Faucet temporarily unavailable — operator needs USDC refill"
        )

    recipient = Pubkey.from_string(body.address)

    # Sequential: SOL first, then USDC
    try:
        sol_sig = await asyncio.to_thread(_send_sol, operator, recipient, FAUCET_SOL)
    except Exception as exc:
        logger.exception("SOL transfer failed for %s", body.address)
        raise HTTPException(502, f"SOL transfer failed: {type(exc).__name__}")

    try:
        usdc_sig = await asyncio.to_thread(
            _send_usdc, operator, recipient, usdc_mint, FAUCET_USDC
        )
    except Exception as exc:
        logger.exception(
            "USDC transfer failed for %s (SOL succeeded: %s)",
            body.address,
            sol_sig,
        )
        _record_claim(body.address, {"partial": True, "sol_tx": sol_sig})
        raise HTTPException(
            502,
            f"USDC transfer failed ({type(exc).__name__}). "
            f"SOL was sent successfully (tx: {sol_sig}).",
        )

    _record_claim(
        body.address,
        {"sol_tx": sol_sig, "usdc_tx": usdc_sig},
    )

    logger.info(
        "Solana faucet: sent SOL+USDC to %s (sol=%s, usdc=%s)",
        body.address,
        sol_sig,
        usdc_sig,
    )

    return SolanaFaucetResponse(
        sol_amount=str(FAUCET_SOL),
        usdc_amount=str(FAUCET_USDC),
        sol_tx_signature=sol_sig,
        usdc_tx_signature=usdc_sig,
    )
