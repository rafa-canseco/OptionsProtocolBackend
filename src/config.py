from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    base_sepolia_rpc_url: str = "https://sepolia.base.org"
    chainlink_eth_usd_address: str = "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1"  # Base Sepolia

    # Pricing defaults
    risk_free_rate: float = 0.05  # 5% annualized
    price_ttl_seconds: int = 30  # how long a quoted price is valid
    batch_interval_minutes: int = 30

    # Circuit breaker
    circuit_breaker_threshold: float = 0.02  # 2% move triggers pause

    model_config = {"env_file": ".env"}


settings = Settings()
