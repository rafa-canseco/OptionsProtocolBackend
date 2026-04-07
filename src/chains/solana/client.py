"""Solana RPC client, keypair, and transaction utilities."""

import json
import logging
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
        data = json.loads(path.read_text())
        _operator = Keypair.from_bytes(bytes(data))
    else:
        _operator = Keypair.from_base58_string(raw)

    logger.info("Solana operator loaded: %s", _operator.pubkey())
    return _operator


def get_pubkey(address: str) -> Pubkey:
    """Parse a base58 string into a Pubkey."""
    return Pubkey.from_string(address)


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
        return 0
    return int(resp.value.amount)


def get_sol_balance(owner: str) -> int:
    """Read native SOL balance in lamports."""
    client = get_solana_client()
    resp = client.get_balance(Pubkey.from_string(owner), commitment=Confirmed)
    return resp.value


def build_and_send_solana_tx(
    tx: VersionedTransaction,
    timeout: int = 60,
) -> str:
    """Send a signed transaction and wait for confirmation.

    Returns the transaction signature as a string.
    """
    client = get_solana_client()
    resp = client.send_transaction(tx)
    sig = str(resp.value)

    client.confirm_transaction(sig, commitment=Confirmed, sleep_seconds=0.5)
    logger.info("Solana tx confirmed: %s", sig)
    return sig
