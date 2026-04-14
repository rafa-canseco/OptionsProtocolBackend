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
    solana_paxg_mint: str = ""
    solana_xau_mint: str = ""

    # Pyth oracle
    solana_pyth_receiver_program: str = ""

    # Solana chain ID (for display only)
    solana_cluster: str = "devnet"

    # ── CCTP V2 (Cross-Chain Transfer Protocol) ──
    # Attestation API — sandbox for testnet, production for mainnet
    cctp_attestation_api_url: str = ""  # set by has_bridge_config default

    # Base CCTP V2 contract addresses
    cctp_base_message_transmitter: str = ""
    cctp_base_token_messenger: str = ""
    cctp_base_domain: int = 6

    # Solana CCTP V2 program IDs (same mainnet/devnet)
    cctp_solana_message_transmitter: str = (
        "CCTPV2Sm4AdWt5296sk4P66VBZ7bEhcARwFaaS9YPbeC"
    )
    cctp_solana_token_messenger: str = "CCTPV2vPZJS2u2BBsUoscuikbYjnpFmbFsvVuJdgUMQe"
    cctp_solana_domain: int = 5

    # Solana USDC mint (mainnet)
    cctp_solana_usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    # Relayer wallets (separate from operator — only for gas)
    relayer_base_private_key: str = ""
    relayer_solana_keypair: str = ""

    # Relayer tuning
    cctp_attestation_poll_interval: int = 3
    cctp_attestation_timeout: int = 300
    cctp_trade_max_retries: int = 3

    # ── XLayer testnet ──
    xlayer_rpc_url: str = ""
    xlayer_wss_rpc_url: str = ""
    xlayer_chain_id: int = 1952

    # XLayer oracle (MockChainlinkFeed)
    chainlink_okb_usd_address: str = "0x0A56056Af2e1157B0787E50B4214d21fB9e7fd5a"
    wokb_address: str = "0x1B5D20CcA8D0B8F5FB25aA06735a57E1B104A1A8"

    # XLayer contract addresses (defaults from deployments-xlayer.json)
    xlayer_usdc_address: str = "0x4A881f3f745B99f0C5575577D80958a5a16b7347"
    xlayer_batch_settler_address: str = "0x6aea5B95d64962E7F001218159cB5fb11712E8B1"
    xlayer_controller_address: str = "0x75701c1A79Ea45F8BDE9A885A84a7581672d4820"
    xlayer_otoken_factory_address: str = "0x7C9418a13462174b2b29bc0B99807A13B9731690"
    xlayer_margin_pool_address: str = "0x3b14faD41CcbD471296e11Ea348dC303aA3A4156"
    xlayer_oracle_address: str = "0xE3E0bcD6ea5b952F98afcb89D848962100127db1"
    xlayer_whitelist_address: str = "0x16e505DBeE21fD1EFDb8402444e70840af6D6FBa"

    # CoinGecko price updater interval
    coingecko_price_update_interval_seconds: int = 60

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


def get_cctp_attestation_url() -> str:
    """Return the Circle attestation API URL, defaulting by beta_mode."""
    if settings.cctp_attestation_api_url:
        return settings.cctp_attestation_api_url
    if settings.beta_mode:
        return "https://iris-api-sandbox.circle.com"
    return "https://iris-api.circle.com"


def has_bridge_config() -> bool:
    """True when CCTP relayer wallets + contracts are configured."""
    return bool(
        settings.cctp_base_message_transmitter
        and settings.cctp_base_token_messenger
        and (settings.relayer_base_private_key or settings.relayer_solana_keypair)
    )


def has_solana_config() -> bool:
    """True when Solana RPC + operator + core programs are configured."""
    return bool(
        settings.solana_rpc_url
        and settings.solana_operator_keypair
        and settings.solana_batch_settler_program_id
        and settings.solana_otoken_factory_program_id
    )


def has_xlayer_config() -> bool:
    """True when XLayer RPC + operator + core contracts are configured."""
    return bool(
        settings.xlayer_rpc_url
        and settings.operator_private_key
        and settings.xlayer_otoken_factory_address
    )
