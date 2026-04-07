"""Address format detection and validation."""

import re

from src.chains import Chain

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Base58 alphabet (no 0, O, I, l)
_B58_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def detect_chain(address: str) -> Chain:
    """Detect chain from address format.

    - 0x-prefixed 40-hex-char → Base (EVM)
    - 32–44 char Base58 → Solana

    Raises ValueError if the address matches neither format.
    """
    if ETH_ADDRESS_RE.match(address):
        return Chain.BASE

    if 32 <= len(address) <= 44 and all(c in _B58_CHARS for c in address):
        return Chain.SOLANA

    raise ValueError(
        f"Unrecognized address format: {address!r}. "
        "Expected 0x-prefixed hex (Base) or Base58 (Solana)."
    )


def is_valid_solana_address(address: str) -> bool:
    """Check if string is a valid Solana base58 address."""
    return 32 <= len(address) <= 44 and all(c in _B58_CHARS for c in address)
