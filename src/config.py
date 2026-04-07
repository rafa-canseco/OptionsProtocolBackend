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
        "0x07DA0E54543a844a80ABE69c8A12F22B3aA59f9D"  # Base mainnet cbBTC/USD
    )

    # Asset addresses (Base mainnet)
    weth_address: str = "0x4200000000000000000000000000000000000006"
    wbtc_address: str = (
        "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"  # Base mainnet cbBTC
    )
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
    treasury_address: str = "0x0744e5Abb82A0337B2F6ac65aC83D1e9861C9740"

    # Custom expiry timestamps override (comma-separated Unix timestamps at 08:00 UTC)
    # e.g. "1773950400,1774123200". If empty, get_expiries() is used.
    custom_expiry_timestamps: str = ""

    # Hours before expiry to stop showing/creating options
    expiry_cutoff_hours: int = 48  # standard (3d/7d/14d)
    short_expiry_cutoff_hours: int = 4  # near-expiry (TTL <= 48h)

    # Expiry settlement
    expiry_settle_hour_utc: int = 8  # 08:00 UTC
    settlement_max_retries: int = 5
    settlement_sweep_interval_seconds: int = 300  # 5 min between sweeps
    settlement_sweep_max_cycles: int = 24  # ~2h of sweeps at 5min intervals

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

    # Base chain
    chain_id: int = 8453  # Base mainnet

    # ── Solana ──
    solana_rpc_url: str = ""
    solana_wss_rpc_url: str = ""
    solana_operator_keypair: str = ""  # base58 private key or path to JSON

    # Solana program IDs
    solana_batch_settler_program_id: str = ""
    solana_controller_program_id: str = ""
    solana_oracle_program_id: str = ""
    solana_otoken_factory_program_id: str = ""
    solana_margin_pool_program_id: str = ""
    solana_whitelist_program_id: str = ""
    solana_address_book_program_id: str = ""

    # Solana token mints
    solana_usdc_mint: str = ""
    solana_wsol_mint: str = "So11111111111111111111111111111111111111112"
    solana_jup_mint: str = ""
    solana_xau_mint: str = ""

    # Pyth oracle
    solana_pyth_receiver_program: str = ""

    # Solana chain ID (for display only)
    solana_cluster: str = "devnet"

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

    # Email notifications (Resend)
    resend_api_key: str = ""
    email_from: str = "b1nary <notifications@b1nary.app>"
    api_base_url: str = "https://api.b1nary.app"  # for absolute URLs in emails
    unsubscribe_secret: str = ""
    notification_check_interval_seconds: int = 1800  # 30 min

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def has_solana_config() -> bool:
    """True when Solana RPC + operator + core programs are configured."""
    return bool(
        settings.solana_rpc_url
        and settings.solana_operator_keypair
        and settings.solana_batch_settler_program_id
        and settings.solana_otoken_factory_program_id
    )
