from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    rpc_url: str = ""
    wss_rpc_url: str = ""  # WSS RPC — enables eth_subscribe when set
    chainlink_eth_usd_address: str = (
        "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"  # Base mainnet
    )
    chainlink_btc_usd_address: str = (
        "0x64c911996D3c6aC71f9b455B1E8E7266BcbD848F"  # Base mainnet
    )

    # Asset addresses (Base mainnet)
    weth_address: str = "0x4200000000000000000000000000000000000006"
    wbtc_address: str = "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c"  # Base mainnet
    usdc_address: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    # Contract addresses (set after deployment)
    batch_settler_address: str = ""
    controller_address: str = ""
    otoken_factory_address: str = ""
    margin_pool_address: str = ""

    # Operator wallet (for bots that send transactions)
    operator_private_key: str = ""

    # Pricing defaults
    risk_free_rate: float = 0.05  # 5% annualized

    # Bot intervals
    otoken_publish_interval_seconds: int = 300  # 5 minutes
    event_poll_interval_seconds: int = 30
    circuit_breaker_poll_seconds: int = 10

    # Circuit breaker
    circuit_breaker_threshold: float = 0.02  # 2% move triggers pause

    # Protocol fee
    protocol_fee_bps: int = 400  # 4% — must match on-chain value

    # Custom expiry timestamps override (comma-separated Unix timestamps at 08:00 UTC)
    # e.g. "1773950400,1774123200". If empty, get_friday_expiries() is used.
    custom_expiry_timestamps: str = ""

    # Hours before expiry to stop showing/creating options (default 48h)
    expiry_cutoff_hours: int = 48

    # Expiry settlement
    expiry_settle_hour_utc: int = 8  # 08:00 UTC

    # Physical settlement (flash loan + DEX swap)
    uniswap_v3_router_address: str = (
        "0x2626664c2603336E57B271c5C0b26F421741e481"  # Base mainnet SwapRouter02
    )
    uniswap_v3_quoter_address: str = (
        "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"  # Base mainnet QuoterV2
    )
    aave_v3_pool_address: str = (
        "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"  # Base mainnet
    )
    uniswap_fee_tier: int = 3000  # 0.3% — most liquid ETH/USDC pool on Base
    swap_slippage_tolerance: float = 0.01  # 1% slippage default
    flash_loan_redeem_delay_seconds: int = 300  # wait 5 min post-settle before delivery

    # Oracle (for reading expiry prices)
    oracle_address: str = ""

    # Whitelist (for whitelisting oTokens after creation)
    whitelist_address: str = ""

    # Chain
    chain_id: int = 8453  # Base mainnet

    # CORS allowed origins (comma-separated). Set to production domain(s) in mainnet.
    allowed_origins: str = "*"

    # Beta mode: disables auto-settlement, enables /demo/settle endpoint
    beta_mode: bool = False
    demo_api_key: str = ""
    mock_chainlink_feed_address: str = ""  # MockSwapRouter's price feed (beta only)

    # Historical P&L / engagement
    coingecko_api_url: str = "https://api.coingecko.com/api/v3"
    weekly_aggregation_day: int = 4  # 0=Monday, 4=Friday
    weekly_aggregation_hour_utc: int = 12  # 12:00 UTC
    eth_staking_apy: float = 0.035  # 3.5% annualized

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
