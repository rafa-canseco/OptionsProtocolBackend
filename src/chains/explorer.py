"""Block explorer URL helpers."""

from src.chains import Chain
from src.config import settings


def tx_explorer_url(tx_hash: str | None, chain: str | Chain | None) -> str | None:
    """Return the canonical transaction explorer URL for a chain/environment."""
    if not tx_hash:
        return None

    chain_value = chain.value if isinstance(chain, Chain) else chain

    if chain_value == Chain.SOLANA.value:
        cluster = (settings.solana_cluster or "mainnet-beta").lower()
        if cluster in ("mainnet", "mainnet-beta"):
            return f"https://solscan.io/tx/{tx_hash}"
        return f"https://solscan.io/tx/{tx_hash}?cluster={cluster}"

    if chain_value == Chain.BASE.value or chain_value is None:
        if settings.chain_id == 84532:
            return f"https://sepolia.basescan.org/tx/{tx_hash}"
        return f"https://basescan.org/tx/{tx_hash}"

    return None
