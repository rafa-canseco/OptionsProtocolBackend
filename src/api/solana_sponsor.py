"""Solana sponsored setup transactions.

The frontend uses this endpoint for SOL covered calls that need a setup step
before execute_order. The operator pays transaction fees and ATA rent while the
user remains the only signer that can move their SOL into their wSOL ATA.
"""

import base64
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.message import MessageV0  # type: ignore[import-untyped]
from solders.null_signer import NullSigner  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.system_program import TransferParams, transfer  # type: ignore[import-untyped]
from solders.transaction import VersionedTransaction  # type: ignore[import-untyped]
from spl.token.constants import (  # type: ignore[import-untyped]
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from spl.token.instructions import (  # type: ignore[import-untyped]
    ApproveParams,
    SyncNativeParams,
    approve,
    create_associated_token_account,
    sync_native,
)

from src.chains.address import is_valid_solana_address
from src.chains.solana.client import get_solana_client, get_solana_operator
from src.config import has_solana_config, settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/solana", tags=["Solana"])

MAX_SPONSORED_WRAP_LAMPORTS = 10 * 10**9


def _derive_ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def _derive_pda(seed: bytes, program_id: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([seed], program_id)[0]


class SponsoredSetupRequest(BaseModel):
    user: str = Field(description="User Solana wallet address")
    otoken_mint: str = Field(description="oToken mint address")
    wrap_lamports: int = Field(ge=0, description="Native SOL lamports to wrap")
    approve_amount: int = Field(gt=0, description="Raw collateral amount to approve")

    @field_validator("user", "otoken_mint")
    @classmethod
    def _valid_pubkey(cls, value: str) -> str:
        if not is_valid_solana_address(value):
            raise ValueError("invalid Solana address")
        return value

    @field_validator("wrap_lamports")
    @classmethod
    def _reasonable_wrap(cls, value: int) -> int:
        if value > MAX_SPONSORED_WRAP_LAMPORTS:
            raise ValueError("wrap_lamports exceeds sponsored setup limit")
        return value


class SponsoredSetupResponse(BaseModel):
    transaction: str = Field(description="Base64 partially signed v0 transaction")
    sponsor: str = Field(description="Operator pubkey that signed as fee payer")


def _build_sponsored_setup_tx(
    operator: Keypair,
    user: Pubkey,
    otoken_mint: Pubkey,
    wrap_lamports: int,
    approve_amount: int,
) -> VersionedTransaction:
    if not settings.solana_batch_settler_program_id:
        raise ValueError("solana_batch_settler_program_id is not configured")
    if not settings.solana_usdc_mint:
        raise ValueError("solana_usdc_mint is not configured")

    rpc = get_solana_client()
    batch_settler = Pubkey.from_string(settings.solana_batch_settler_program_id)
    usdc_mint = Pubkey.from_string(settings.solana_usdc_mint)
    wsol_mint = Pubkey.from_string(settings.solana_wsol_mint)
    settler_config = _derive_pda(b"settler_config", batch_settler)

    settler_otoken_account = _derive_ata(settler_config, otoken_mint)
    user_premium_account = _derive_ata(user, usdc_mint)
    user_wsol_account = _derive_ata(user, wsol_mint)

    instructions = []
    if rpc.get_account_info(settler_otoken_account).value is None:
        instructions.append(
            create_associated_token_account(
                payer=operator.pubkey(),
                owner=settler_config,
                mint=otoken_mint,
            )
        )

    if rpc.get_account_info(user_premium_account).value is None:
        instructions.append(
            create_associated_token_account(
                payer=operator.pubkey(),
                owner=user,
                mint=usdc_mint,
            )
        )

    if rpc.get_account_info(user_wsol_account).value is None:
        instructions.append(
            create_associated_token_account(
                payer=operator.pubkey(),
                owner=user,
                mint=wsol_mint,
            )
        )

    if wrap_lamports > 0:
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=user,
                    to_pubkey=user_wsol_account,
                    lamports=wrap_lamports,
                )
            )
        )
        instructions.append(
            sync_native(
                SyncNativeParams(
                    program_id=TOKEN_PROGRAM_ID,
                    account=user_wsol_account,
                )
            )
        )

    instructions.append(
        approve(
            ApproveParams(
                program_id=TOKEN_PROGRAM_ID,
                source=user_wsol_account,
                delegate=settler_config,
                owner=user,
                amount=approve_amount,
                signers=[],
            )
        )
    )

    blockhash = rpc.get_latest_blockhash().value.blockhash
    msg = MessageV0.try_compile(
        payer=operator.pubkey(),
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    return VersionedTransaction(msg, [operator, NullSigner(user)])


@router.post(
    "/sponsored-setup",
    response_model=SponsoredSetupResponse,
    summary="Build a sponsored Solana trade setup transaction",
)
async def sponsored_setup(body: SponsoredSetupRequest) -> SponsoredSetupResponse:
    if not has_solana_config():
        raise HTTPException(503, "Solana sponsorship is not configured")

    try:
        operator = get_solana_operator()
        tx = _build_sponsored_setup_tx(
            operator=operator,
            user=Pubkey.from_string(body.user),
            otoken_mint=Pubkey.from_string(body.otoken_mint),
            wrap_lamports=body.wrap_lamports,
            approve_amount=body.approve_amount,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to build sponsored Solana setup transaction")
        raise HTTPException(500, "Failed to build sponsored setup transaction") from exc

    return SponsoredSetupResponse(
        transaction=base64.b64encode(bytes(tx)).decode("ascii"),
        sponsor=str(operator.pubkey()),
    )
