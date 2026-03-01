"""MM client configuration.

Loaded from .env.mm (or environment variables). Completely independent
of the backend's src.config.settings.
"""
from pydantic_settings import BaseSettings


class MMClientSettings(BaseSettings):
    # b1nary API (only for oToken discovery + quote submission)
    api_base_url: str = "http://localhost:8000"
    api_key: str

    # MM wallet
    mm_private_key: str

    # On-chain config (for EIP-712 signing + makerNonce reads)
    batch_settler_address: str
    chain_id: int = 84532
    rpc_url: str = "https://sepolia.base.org"

    # MM's own price feed (Chainlink on Base Sepolia)
    chainlink_eth_usd_address: str = (
        "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1"
    )

    # Pricing params
    spread: float = 0.01
    risk_free_rate: float = 0.05
    publish_interval_seconds: int = 300
    quote_deadline_seconds: int = 1800
    max_amount_otokens: int = 1000_0000_0000  # 1000 oTokens (8 decimals)

    model_config = {"env_file": ".env.mm"}
