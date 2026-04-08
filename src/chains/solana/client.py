"""Solana RPC client, keypair, and transaction utilities."""

import json
import logging
import struct
from pathlib import Path

from solana.rpc.api import Client as SolanaClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.transaction import VersionedTransaction  # type: ignore[import-untyped]

from src.config import settings

logger = logging.getLogger(__name__)

_client: SolanaClient | None = None
_operator: Keypair | None = None


def get_solana_client() -> SolanaClient:
    """Lazy-init synchronous Solana RPC client."""
    global _client
    if _client is None:
        if not settings.solana_rpc_url:
            raise ValueError(
                "solana_rpc_url is not configured. Set SOLANA_RPC_URL env var."
            )
        _client = SolanaClient(settings.solana_rpc_url)
    return _client


def get_solana_operator() -> Keypair:
    """Load operator keypair from config.

    Accepts either:
    - A path to a JSON file containing a byte array (Solana CLI format)
    - A base58-encoded secret key string
    """
    global _operator
    if _operator is not None:
        return _operator

    raw = settings.solana_operator_keypair
    if not raw:
        raise ValueError(
            "solana_operator_keypair is not configured. "
            "Set SOLANA_OPERATOR_KEYPAIR env var."
        )

    path = Path(raw)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            _operator = Keypair.from_bytes(bytes(data))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Failed to load Solana keypair from {path}. "
                "Expected a JSON file with a byte array "
                "(Solana CLI format)."
            ) from exc
    else:
        try:
            _operator = Keypair.from_base58_string(raw)
        except Exception as exc:
            raise ValueError(
                "Failed to parse SOLANA_OPERATOR_KEYPAIR as base58. "
                "Provide a valid base58 secret key or path to a "
                "JSON keyfile."
            ) from exc

    logger.info("Solana operator loaded: %s", _operator.pubkey())
    return _operator


def get_pubkey(address: str) -> Pubkey:
    """Parse a base58 string into a Pubkey."""
    return Pubkey.from_string(address)


def _check_rpc_error(resp, context: str) -> None:
    """Raise if a Solana RPC response contains an error."""
    if hasattr(resp, "error") and resp.error:
        raise RuntimeError(f"Solana RPC error ({context}): {resp.error}")


def get_balance(owner: str, mint: str) -> int:
    """Read SPL token balance for owner. Returns raw amount (0 if no ATA)."""
    from solders.pubkey import Pubkey as Pk  # type: ignore[import-untyped]
    from spl.token.constants import TOKEN_PROGRAM_ID  # type: ignore[import-untyped]

    client = get_solana_client()
    owner_pk = Pk.from_string(owner)
    mint_pk = Pk.from_string(mint)

    # Derive Associated Token Address
    ata = Pk.find_program_address(
        [
            bytes(owner_pk),
            bytes(TOKEN_PROGRAM_ID),
            bytes(mint_pk),
        ],
        Pk.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
    )[0]

    resp = client.get_token_account_balance(ata)
    if resp.value is None:
        _check_rpc_error(resp, f"get_token_account_balance({owner[:8]}...)")
        return 0  # ATA does not exist
    return int(resp.value.amount)


def get_sol_balance(owner: str) -> int:
    """Read native SOL balance in lamports."""
    client = get_solana_client()
    try:
        resp = client.get_balance(Pubkey.from_string(owner), commitment=Confirmed)
    except Exception as exc:
        raise RuntimeError(f"Failed to read SOL balance for {owner[:8]}...") from exc
    _check_rpc_error(resp, f"get_balance({owner[:8]}...)")
    return resp.value


_MAKER_NONCE_OFFSET = 8 + 32  # discriminator + maker pubkey = 40


def get_solana_maker_nonce(maker_pubkey: str) -> int:
    """Read the nonce from a MakerState PDA for the given maker.

    Derives the PDA with seeds [b"maker", bytes(maker_pk)] under the
    batch settler program, then unpacks the nonce field at byte offset 40
    (8-byte Anchor discriminator + 32-byte maker pubkey).

    Returns 0 if the account has not been created yet.
    Raises RuntimeError on RPC failure.
    """
    maker_pk = Pubkey.from_string(maker_pubkey)
    program_pk = Pubkey.from_string(settings.solana_batch_settler_program_id)

    pda, _ = Pubkey.find_program_address(
        [b"maker", bytes(maker_pk)],
        program_pk,
    )

    client = get_solana_client()
    try:
        resp = client.get_account_info(pda)
    except Exception as exc:
        raise RuntimeError(
            f"Solana RPC error reading MakerState for {maker_pubkey[:8]}..."
        ) from exc

    if resp.value is None:
        return 0

    data: bytes = resp.value.data
    nonce: int = struct.unpack_from("<Q", data, _MAKER_NONCE_OFFSET)[0]
    return nonce


def build_and_send_solana_tx(tx: VersionedTransaction) -> str:
    """Send a signed transaction and wait for confirmation.

    Returns the transaction signature as a string.
    """
    client = get_solana_client()
    try:
        resp = client.send_transaction(tx)
    except Exception as exc:
        raise RuntimeError("Failed to send Solana transaction") from exc

    sig = str(resp.value)
    try:
        client.confirm_transaction(sig, commitment=Confirmed, sleep_seconds=0.5)
    except Exception as exc:
        logger.error("Tx sent but confirmation failed: %s", sig)
        raise RuntimeError(f"Transaction {sig} sent but confirmation failed") from exc

    logger.info("Solana tx confirmed: %s", sig)
    return sig
