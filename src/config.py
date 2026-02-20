from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    base_sepolia_rpc_url: str = "https://sepolia.base.org"
    chainlink_eth_usd_address: str = "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1"  # Base Sepolia

    # Asset addresses (Base Sepolia)
    weth_address: str = "0x4200000000000000000000000000000000000006"
    usdc_address: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

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

    # Protocol fee
    protocol_fee_bps: int = 400  # 4% — must match on-chain value

    # Expiry settlement
    expiry_settle_hour_utc: int = 8  # 08:00 UTC

    # Physical settlement (flash loan + DEX swap)
    uniswap_v3_router_address: str = ""
    uniswap_v3_quoter_address: str = ""
    aave_v3_pool_address: str = ""
    swap_slippage_tolerance: float = 0.01  # 1% slippage default
    flash_loan_redeem_delay_seconds: int = 300  # wait 5 min post-settle before delivery

    # Oracle (for reading expiry prices)
    oracle_address: str = ""

    # Chain
    chain_id: int = 84532  # Base Sepolia

    # Beta mode: disables auto-settlement, enables /demo/settle endpoint
    beta_mode: bool = False
    demo_api_key: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
