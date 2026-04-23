from unittest.mock import patch

from src.api.routes import _enrich_positions
from src.chains.explorer import tx_explorer_url


def test_solana_devnet_tx_url_includes_cluster():
    with patch("src.chains.explorer.settings.solana_cluster", "devnet"):
        url = tx_explorer_url("sig123", "solana")

    assert url == "https://solscan.io/tx/sig123?cluster=devnet"


def test_solana_mainnet_tx_url_omits_cluster():
    with patch("src.chains.explorer.settings.solana_cluster", "mainnet-beta"):
        url = tx_explorer_url("sig123", "solana")

    assert url == "https://solscan.io/tx/sig123"


def test_base_sepolia_tx_url_uses_sepolia_basescan():
    with patch("src.chains.explorer.settings.chain_id", 84532):
        url = tx_explorer_url("0xabc", "base")

    assert url == "https://sepolia.basescan.org/tx/0xabc"


def test_enrich_positions_adds_explorer_urls():
    positions = [
        {
            "tx_hash": "sig123",
            "chain": "solana",
            "is_settled": False,
            "net_premium": "100",
        }
    ]

    with patch("src.chains.explorer.settings.solana_cluster", "devnet"):
        enriched = _enrich_positions(positions)

    assert enriched[0]["tx_url"] == "https://solscan.io/tx/sig123?cluster=devnet"
    assert enriched[0]["explorer_url"] == enriched[0]["tx_url"]
    assert enriched[0]["premium"] == "100"
