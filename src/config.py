from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    base_sepolia_rpc_url: str = "https://sepolia.base.org"
    chainlink_eth_usd_address: str = "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1"  # Base Sepolia

    # Contract addresses (set after deployment)
    price_sheet_address: str = ""
    batch_settler_address: str = ""
    controller_address: str = ""
    otoken_factory_address: str = ""

    # Operator wallet (for bots that send transactions)
    operator_private_key: str = ""

    # Pricing defaults
    risk_free_rate: float = 0.05  # 5% annualized
    price_ttl_seconds: int = 30  # how long a quoted price is valid

    # Bot intervals
    price_publish_interval_seconds: int = 300  # 5 minutes
    event_poll_interval_seconds: int = 15
    circuit_breaker_poll_seconds: int = 10

    # Quote settings
    quote_deadline_seconds: int = 600  # 10 min deadline for on-chain quotes
    default_max_amount_wei: int = 10_000_000_000_000_000_000  # 10 ETH in wei

    # Circuit breaker
    circuit_breaker_threshold: float = 0.02  # 2% move triggers pause

    # Expiry settlement
    expiry_settle_hour_utc: int = 8  # 08:00 UTC

    # Chain
    chain_id: int = 84532  # Base Sepolia

    model_config = {"env_file": ".env"}


settings = Settings()
