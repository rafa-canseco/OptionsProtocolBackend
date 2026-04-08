"""Ed25519 signature verification for Solana quote messages."""

import logging
import struct

from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.signature import Signature  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def build_solana_quote_message(
    otoken_mint: bytes,
    *,
    bid_price: int,
    deadline: int,
    quote_id: int,
    max_amount: int,
    maker_nonce: int,
) -> bytes:
    """Pack a 72-byte message matching the Solana BatchSettler layout.

    Layout: otoken_mint (32B) | bid_price (u64 LE) | deadline (i64 LE)
            | quote_id (u64 LE) | max_amount (u64 LE) | maker_nonce (u64 LE)

    Args:
        otoken_mint: 32-byte mint address.
        bid_price: Quote bid price as unsigned 64-bit integer.
        deadline: Quote deadline as signed 64-bit integer (Unix timestamp).
        quote_id: Unique quote identifier.
        max_amount: Maximum fill amount.
        maker_nonce: Maker's on-chain nonce.

    Raises:
        ValueError: If otoken_mint is not exactly 32 bytes.
    """
    if len(otoken_mint) != 32:
        raise ValueError(
            f"otoken_mint must be 32 bytes, got {len(otoken_mint)}. "
            "Pass the raw bytes of the Solana mint address."
        )
    return (
        otoken_mint
        + struct.pack("<Q", bid_price)
        + struct.pack("<q", deadline)
        + struct.pack("<Q", quote_id)
        + struct.pack("<Q", max_amount)
        + struct.pack("<Q", maker_nonce)
    )


def verify_solana_quote(pubkey: Pubkey, message: bytes, signature: bytes) -> bool:
    """Verify an ed25519 signature over a Solana quote message.

    Args:
        pubkey: The signer's public key.
        message: The raw message bytes that were signed.
        signature: The 64-byte ed25519 signature.

    Returns:
        True if the signature is valid, False otherwise. Never raises.
    """
    try:
        return Signature.from_bytes(signature).verify(pubkey, message)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Unexpected error during ed25519 verification",
            exc_info=True,
        )
        return False
