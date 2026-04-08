"""
Solana Circuit Breaker Bot

Monitors Pyth spot price for SOL. When the circuit breaker trips
(>2% move), calls increment_maker_nonce on the Solana BatchSettler
to invalidate on-chain quotes, and deactivates Solana quotes in DB.
"""

import asyncio
import hashlib
import logging

from solana.rpc.commitment import Confirmed
from solders.instruction import AccountMeta, Instruction  # type: ignore[import-untyped]
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.message import MessageV0  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.transaction import VersionedTransaction  # type: ignore[import-untyped]

from src.chains.solana.client import (
    build_and_send_solana_tx,
    get_solana_client,
    get_solana_operator,
)
from src.chains.solana.oracle import get_pyth_price
from src.config import settings
from src.db.database import get_client
from src.pricing.assets import Asset
from src.pricing.circuit_breaker import circuit_breaker

logger = logging.getLogger(__name__)

_NONCE_DISCRIMINATOR = hashlib.sha256(b"global:increment_maker_nonce").digest()[:8]


def build_increment_nonce_ix(operator_pubkey: Pubkey) -> Instruction:
    """Build the increment_maker_nonce Anchor instruction.

    Accounts: maker_state PDA (writable) + maker/operator (signer).
    Data: 8-byte Anchor discriminator only (no args).
    """
    program_id = Pubkey.from_string(settings.solana_batch_settler_program_id)
    maker_state_pda, _ = Pubkey.find_program_address(
        [b"maker", bytes(operator_pubkey)],
        program_id,
    )
    return Instruction(
        program_id=program_id,
        accounts=[
            AccountMeta(
                pubkey=maker_state_pda,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=operator_pubkey,
                is_signer=True,
                is_writable=False,
            ),
        ],
        data=_NONCE_DISCRIMINATOR,
    )


def _send_increment_nonce_tx(operator: Keypair, ix: Instruction) -> str:
    """Build and send increment_maker_nonce tx (sync, runs in thread)."""
    rpc = get_solana_client()
    blockhash = rpc.get_latest_blockhash(commitment=Confirmed).value.blockhash
    msg = MessageV0.try_compile(operator.pubkey(), [ix], [], blockhash)
    tx = VersionedTransaction(msg, [operator])
    return build_and_send_solana_tx(tx)


async def invalidate_solana_quotes(asset: str) -> None:
    """Increment on-chain nonce + deactivate Solana quotes in DB."""
    operator = get_solana_operator()
    ix = build_increment_nonce_ix(operator.pubkey())

    try:
        sig = await asyncio.to_thread(_send_increment_nonce_tx, operator, ix)
        logger.warning(
            "Solana circuit breaker (%s): incremented makerNonce, tx: %s",
            asset,
            sig,
        )
    except Exception:
        logger.exception(
            "CRITICAL: Solana circuit breaker (%s) failed to increment "
            "makerNonce. Signed quotes remain valid.",
            asset,
        )
        raise

    client = get_client()
    result = (
        client.table("mm_quotes")
        .update({"is_active": False})
        .eq("is_active", True)
        .eq("chain", "solana")
        .execute()
    )
    deactivated = len(result.data) if result.data else 0
    logger.warning(
        "Solana circuit breaker (%s): deactivated %d DB quotes",
        asset,
        deactivated,
    )


async def check_once() -> None:
    """Check SOL price. If tripped, invalidate quotes."""
    asset = Asset.SOL
    try:
        price, _ = get_pyth_price(asset)
    except Exception:
        logger.exception(
            "Solana circuit breaker: failed to read %s price from Pyth. "
            "Safety check skipped.",
            asset.value,
        )
        return

    if circuit_breaker.check(price, asset.value):
        reason = circuit_breaker.pause_reason_for(asset.value)
        logger.warning("Solana circuit breaker tripped: %s", reason)
        await invalidate_solana_quotes(asset.value)
        circuit_breaker.update_reference(price, asset.value)


async def run() -> None:
    """Main loop: check SOL price every N seconds."""
    logger.info(
        "Solana circuit breaker bot starting (interval=%ds, asset=SOL)",
        settings.circuit_breaker_poll_seconds,
    )
    while True:
        try:
            await check_once()
        except Exception:
            logger.exception("Solana circuit breaker check failed")
        await asyncio.sleep(settings.circuit_breaker_poll_seconds)
