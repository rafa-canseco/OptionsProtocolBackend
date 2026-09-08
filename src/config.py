import functools
from typing import Optional
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "dev"
    background_workers_enabled: bool = True
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    position_cursor_secret: str = ""
    rpc_url: str = ""
    tokenized_fund_rpc_url: str = ""
    wss_rpc_url: str = ""  # WSS RPC — enables eth_subscribe when set
    chainlink_eth_usd_address: str = (
        "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70"  # Base mainnet
    )
    chainlink_btc_usd_address: str = (
        "0x07DA0E54543a844a80ABE69c8A12F22B3aA59f9D"  # Base mainnet cbBTC/USD
    )
    chainlink_nvdac_usd_address: str = "0x04689a41629776563E6822F76f2e57D148d28513"
    chainlink_nvdac_usd_description: str = "Coinbase NVDA"
    chainlink_vvv_usd_address: str = "0xaABc55Ca55D70B034e4daA2551A224239890282F"
    chainlink_vvv_usd_description: str = "VVV / USD"
    chainlink_hype_usd_hyperevm_address: str = (
        "0xa5a72eF19F82A579431186402425593a559ed352"
    )
    chainlink_zec_usd_arbitrum_address: str = (
        "0x21082CA28570f0ccfb089465bFaEfDc77b00D367"
    )
    chainlink_oracle_max_age_seconds: int = Field(default=3600, gt=0, le=3600)
    base_sequencer_uptime_feed_address: str = (
        "0xBCF85224fc0756B9Fa45aA7892530B47e10b6433"
    )
    arbitrum_rpc_url: str = ""
    arbitrum_chain_id: int = 42161
    arbitrum_sequencer_uptime_feed_address: str = (
        "0xFdB631F5EE196F0ed6FAa767959853A9F217697D"
    )
    arbitrum_sequencer_grace_period_seconds: int = 3600
    hyperevm_rpc_url: str = ""
    hyperevm_chain_id: int = 999

    # Asset addresses (Base mainnet). Registry membership does not enable markets.
    weth_address: str = "0x4200000000000000000000000000000000000006"
    wbtc_address: str = (
        "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"  # Base mainnet cbBTC
    )
    nvdac_address: str = "0xb20000000000000000000078ee7ce2fE4908108C"
    cbzec_address: str = "0xB2000000000000000000008501b13360000cb2EC"
    cbhype_address: str = "0xB200000000000000000000451d033a5000cb479e"
    vvv_address: str = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
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

    # Asset/chain exposure controls.
    # Visible = endpoints may return read-only market data.
    # Tradable = backend may return execution data or submit trades.
    visible_assets: str = "eth,btc,sol,tslax"
    tradable_assets: Optional[str] = None
    tradable_chains: Optional[str] = None

    # Bot intervals
    otoken_publish_interval_seconds: int = 300  # 5 minutes
    # oToken publication/materialization rollout.
    # eager: current behavior; shadow: publish lifecycle metadata but still create;
    # lazy: publish deterministic future addresses and create only on execution intent.
    otoken_series_mode: str = "eager"
    otoken_lazy_assets: str = "eth"
    otoken_materialization_lease_seconds: int = 180
    otoken_materialization_max_attempts: int = 3
    otoken_ensure_deadline_buffer_seconds: int = 30
    otoken_materialization_deadline_buffer_seconds: int = 150
    otoken_ensure_retry_after_ms: int = 750
    otoken_min_trade_amount_raw: int = 1_000_000  # 0.01 oToken (8 decimals)
    otoken_capacity_stale_seconds: int = 120
    otoken_materialization_hourly_limit: int = 6
    otoken_materialization_daily_limit: int = 20
    otoken_materialization_series_hourly_limit: int = 20
    otoken_intent_hmac_secret: str = ""

    # Privy end-user authentication for gas-spending endpoints.
    privy_app_id: str = ""
    privy_app_secret: str = ""
    privy_jwt_verification_key: str = ""
    privy_jwks_url: str = ""
    privy_api_url: str = "https://api.privy.io"
    privy_user_cache_seconds: int = 60

    event_poll_interval_seconds: int = 30
    tokenized_fund_indexer_enabled: bool = False
    tokenized_fund_indexer_poll_interval_seconds: int = 30
    rpc_snapshot_collector_enabled: bool = False
    multicall3_address: str = "0xcA11bde05977b3631167028862bE2a173976CA11"
    fund_nav_reporter_enabled: bool = False
    fund_nav_reporter_interval_seconds: int = 300
    fund_nav_reporter_tx_timeout_seconds: int = 120
    fund_nav_reporter_lease_seconds: int = 180
    fund_nav_reporter_private_keys: str = ""
    fund_nav_submitter_private_key: str = ""
    fund_nav_inclusion_margin_blocks: int = 3
    fund_nav_execution_buffer_blocks: int = 15
    meta_wheel_fund_key: str = ""
    meta_wheel_operator_private_key: str = ""
    meta_wheel_nav_reporter_private_keys: str = ""
    meta_wheel_csp_sepolia_fair_value_observations_enabled: bool = False
    meta_wheel_csp_sepolia_observer_private_keys: str = ""
    meta_wheel_covered_call_sepolia_fair_value_observations_enabled: bool = False
    meta_wheel_covered_call_sepolia_observer_private_keys: str = ""
    fund_csp_sepolia_fair_value_observations_enabled: bool = False
    fund_csp_sepolia_observer_private_keys: str = ""
    fund_csp_sepolia_fair_value_iv_bps: int = 0
    fund_csp_sepolia_fair_value_iv_source: str = ""
    fund_csp_sepolia_fair_value_risk_free_rate_bps: int = 0
    fund_csp_sepolia_fair_value_settlement_cost_bps: int = 0
    fund_covered_call_sepolia_fair_value_observations_enabled: bool = False
    fund_covered_call_sepolia_observer_private_keys: str = ""
    fund_covered_call_sepolia_fair_value_iv_bps: int = 0
    fund_covered_call_sepolia_fair_value_iv_source: str = ""
    fund_covered_call_sepolia_fair_value_risk_free_rate_bps: int = 0
    fund_covered_call_sepolia_fair_value_settlement_cost_bps: int = 0
    fund_state_freshness_seconds: int = 180
    confirmed_head_freshness_seconds: int = 90
    circuit_breaker_poll_seconds: int = 10

    # Circuit breaker
    circuit_breaker_threshold: float = 0.02  # 2% move triggers pause

    # Protocol fee
    protocol_fee_bps: int = 400  # 4% — must match on-chain value
    solana_protocol_fee_bps: int = 400
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

    # New Base settlement routes are fail-closed until explicitly configured/deployed.
    routed_settlement_enabled: bool = False
    routed_settlement_assets: str = ""
    routed_settlement_publishing_enabled: bool = False
    pair_routing_swap_router_address: str = ""
    nvdac_settlement_adapter_address: str = ""
    cbzec_settlement_adapter_address: str = ""
    cbhype_settlement_adapter_address: str = ""
    vvv_settlement_adapter_address: str = ""
    aerodrome_slipstream_factory_address: str = (
        "0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef"
    )
    aerodrome_slipstream_router_address: str = (
        "0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F"
    )
    aerodrome_slipstream_quoter_address: str = (
        "0x514c8B5f54112481E28028F1166Bd78501089259"
    )
    nvdac_pool_address: str = "0x853F5f1B92b16714Fe6CDA67CAad0856B83C7ab9"
    cbzec_pool_address: str = "0x0Fc47C17AF86078d809358db1b4db2DeBC988566"
    cbhype_pool_address: str = "0xD5Eaea9da564217EA101D1E369fDA168A3025686"
    vvv_pool_address: str = "0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530"
    uniswap_v3_factory_address: str = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
    b20_oracle_registry_address: str = "0x3f3E8cf41cdd3b1D118c16471aB0113DfDDd5CaD"
    b20_policy_registry_address: str = "0x8453000000000000000000000000000000000002"

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
    solana_tslax_mint: str = "H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF"

    # Solana swap routing for physical settlement
    solana_jupiter_quote_api_url: str = "https://lite-api.jup.ag/swap/v1"

    # Pyth oracle
    solana_pyth_receiver_program: str = ""

    # Solana chain ID (for display only)
    solana_cluster: str = "devnet"

    # Solana runtime gates.
    # API/read-only exposure is controlled by has_solana_config() plus route-level checks.
    # Background bot runtime is controlled separately so production can stay read-only by default.
    solana_bots_enabled: Optional[bool] = None
    solana_circuit_breaker_bot_enabled: Optional[bool] = None
    solana_event_indexer_enabled: Optional[bool] = None
    solana_expiry_settler_enabled: Optional[bool] = None
    solana_otoken_manager_enabled: Optional[bool] = None

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

    # CORS allowed origins (comma-separated). Set to production domain(s) in mainnet.
    allowed_origins: str = "*"

    # Beta mode: disables auto-settlement, enables /demo/settle endpoint
    beta_mode: bool = False
    demo_api_key: str = ""
    mock_chainlink_feed_address: str = ""  # MockSwapRouter's price feed (beta only)

    # Historical P&L / engagement
    # Temporary rollback gate for the v1 results, leaderboard, and weekly snapshot.
    legacy_agora_v1_enabled: bool = False
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


def get_protocol_fee_bps(chain: str) -> int:
    """Return the settlement fee for the requested chain boundary."""
    if chain.strip().lower() == "solana":
        return settings.solana_protocol_fee_bps
    return settings.protocol_fee_bps


def get_tokenized_fund_rpc_url() -> str:
    """Return the isolated fund RPC when configured, otherwise the global RPC."""
    return settings.tokenized_fund_rpc_url.strip() or settings.rpc_url.strip()


_PUBLIC_BASE_RPC_HOSTS = {
    "baserpcgateway-production.up.railway.app",
    "mainnet.base.org",
}
_PUBLIC_RPC_SUFFIXES = (".alchemy.com", ".alchemyapi.io", ".drpc.live")


def validate_rpc_url(name: str, value: str, schemes: set[str]) -> None:
    """Reject malformed or public Base endpoints without echoing secrets."""
    if not value:
        return
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
        valid = (
            parsed.scheme.lower() in schemes
            and bool(hostname)
            and (port is None or 1 <= port <= 65_535)
        )
    except ValueError:
        valid = False
        hostname = ""
    if not valid:
        raise ValueError(f"{name} must be a valid {'/'.join(sorted(schemes))} URL")
    if hostname in _PUBLIC_BASE_RPC_HOSTS or hostname.endswith(_PUBLIC_RPC_SUFFIXES):
        raise ValueError(f"{name} must use the private authenticated Base RPC route")


def validate_backend_rpc_config() -> None:
    """Validate backend transports without exposing endpoint values in errors."""
    validate_rpc_url("RPC_URL", settings.rpc_url.strip(), {"http", "https"})
    validate_rpc_url("WSS_RPC_URL", settings.wss_rpc_url.strip(), {"ws", "wss"})
    validate_rpc_url(
        "TOKENIZED_FUND_RPC_URL",
        settings.tokenized_fund_rpc_url.strip(),
        {"http", "https"},
    )


def get_fund_nav_reporter_private_keys() -> tuple[str, ...]:
    """Return validated, deduplicated reporter keys."""
    from eth_account import Account

    keys = tuple(
        key.strip()
        for key in settings.fund_nav_reporter_private_keys.split(",")
        if key.strip()
    )
    if not keys:
        raise ValueError("FUND_NAV_REPORTER_PRIVATE_KEYS contains no keys")
    addresses = []
    for key in keys:
        try:
            addresses.append(Account.from_key(key).address.lower())
        except Exception as exc:
            raise ValueError("Invalid FUND_NAV_REPORTER_PRIVATE_KEYS entry") from exc
    if len(addresses) != len(set(addresses)):
        raise ValueError("FUND_NAV_REPORTER_PRIVATE_KEYS contains duplicate reporters")
    return keys


def get_fund_nav_submitter_private_key() -> str:
    """Return the dedicated key used only to submit signed NAV reports."""
    from eth_account import Account

    key = settings.fund_nav_submitter_private_key.strip()
    if not key:
        raise ValueError("FUND_NAV_SUBMITTER_PRIVATE_KEY is required")
    try:
        Account.from_key(key)
    except Exception as exc:
        raise ValueError("Invalid FUND_NAV_SUBMITTER_PRIVATE_KEY") from exc
    return key


def get_meta_wheel_nav_reporter_private_keys() -> tuple[str, ...]:
    """Return the dedicated two-key reporter quorum for the Meta Wheel."""
    from eth_account import Account

    keys = tuple(
        key.strip()
        for key in settings.meta_wheel_nav_reporter_private_keys.split(",")
        if key.strip()
    )
    if len(keys) != 2:
        raise ValueError(
            "META_WHEEL_NAV_REPORTER_PRIVATE_KEYS must contain exactly two keys"
        )
    addresses = []
    for key in keys:
        try:
            addresses.append(Account.from_key(key).address.lower())
        except Exception as exc:
            raise ValueError(
                "Invalid META_WHEEL_NAV_REPORTER_PRIVATE_KEYS entry"
            ) from exc
    if len(addresses) != len(set(addresses)):
        raise ValueError(
            "META_WHEEL_NAV_REPORTER_PRIVATE_KEYS contains duplicate reporters"
        )
    return keys


def get_meta_wheel_observer_private_keys(strategy_kind: str) -> tuple[str, ...]:
    """Return the Wheel-only observer quorum without replacing standalone keys."""
    from eth_account import Account

    if strategy_kind == "csp":
        raw = settings.meta_wheel_csp_sepolia_observer_private_keys
        variable = "META_WHEEL_CSP_SEPOLIA_OBSERVER_PRIVATE_KEYS"
    elif strategy_kind == "covered_call":
        raw = settings.meta_wheel_covered_call_sepolia_observer_private_keys
        variable = "META_WHEEL_COVERED_CALL_SEPOLIA_OBSERVER_PRIVATE_KEYS"
    else:
        raise ValueError("Unsupported Meta Wheel observer strategy")
    keys = tuple(key.strip() for key in raw.split(",") if key.strip())
    if len(keys) != 2:
        raise ValueError(f"{variable} must contain exactly two keys")
    addresses = []
    for key in keys:
        try:
            addresses.append(Account.from_key(key).address.lower())
        except Exception as exc:
            raise ValueError(f"Invalid {variable} entry") from exc
    if len(addresses) != len(set(addresses)):
        raise ValueError(f"{variable} contains duplicate observers")
    return keys


def validate_meta_wheel_credential_topology() -> None:
    """Require every Wheel signing domain to be distinct from all others."""
    from eth_account import Account

    operator_key = settings.meta_wheel_operator_private_key.strip()
    if not operator_key:
        raise ValueError("META_WHEEL_OPERATOR_PRIVATE_KEY is required")
    try:
        operator = Account.from_key(operator_key).address.lower()
    except Exception as exc:
        raise ValueError("Invalid META_WHEEL_OPERATOR_PRIVATE_KEY") from exc

    groups = {
        "Meta Wheel operator": {operator},
        "Meta Wheel NAV reporters": {
            Account.from_key(key).address.lower()
            for key in get_meta_wheel_nav_reporter_private_keys()
        },
        "Meta Wheel CSP observers": {
            Account.from_key(key).address.lower()
            for key in get_meta_wheel_observer_private_keys("csp")
        },
        "Meta Wheel covered-call observers": {
            Account.from_key(key).address.lower()
            for key in get_meta_wheel_observer_private_keys("covered_call")
        },
    }
    optional = (
        (
            "standalone NAV reporters",
            settings.fund_nav_reporter_private_keys,
            get_fund_nav_reporter_private_keys,
        ),
        (
            "standalone NAV submitter",
            settings.fund_nav_submitter_private_key,
            lambda: (get_fund_nav_submitter_private_key(),),
        ),
        (
            "standalone CSP observers",
            settings.fund_csp_sepolia_observer_private_keys,
            get_fund_csp_sepolia_observer_private_keys,
        ),
        (
            "standalone covered-call observers",
            settings.fund_covered_call_sepolia_observer_private_keys,
            get_fund_covered_call_sepolia_observer_private_keys,
        ),
    )
    for label, configured, getter in optional:
        if configured.strip():
            groups[label] = {Account.from_key(key).address.lower() for key in getter()}

    names = tuple(groups)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            if groups[left] & groups[right]:
                raise ValueError(f"Credential overlap between {left} and {right}")


def get_fund_csp_sepolia_observer_private_keys() -> tuple[str, ...]:
    """Return the two dedicated keys for the Base Sepolia fair-value policy."""
    from eth_account import Account

    keys = tuple(
        key.strip()
        for key in settings.fund_csp_sepolia_observer_private_keys.split(",")
        if key.strip()
    )
    if len(keys) != 2:
        raise ValueError(
            "FUND_CSP_SEPOLIA_OBSERVER_PRIVATE_KEYS must contain exactly two keys"
        )
    addresses = []
    for key in keys:
        try:
            addresses.append(Account.from_key(key).address.lower())
        except Exception as exc:
            raise ValueError(
                "Invalid FUND_CSP_SEPOLIA_OBSERVER_PRIVATE_KEYS entry"
            ) from exc
    if len(addresses) != len(set(addresses)):
        raise ValueError(
            "FUND_CSP_SEPOLIA_OBSERVER_PRIVATE_KEYS contains duplicate observers"
        )
    return keys


def get_fund_csp_sepolia_fair_value_policy():
    """Return the strict, explicitly versioned Base Sepolia fair-value policy."""
    from src.fund_nav.fair_value import FairValuePolicy

    return FairValuePolicy(
        implied_volatility_bps=settings.fund_csp_sepolia_fair_value_iv_bps,
        implied_volatility_source=settings.fund_csp_sepolia_fair_value_iv_source,
        risk_free_rate_bps=(settings.fund_csp_sepolia_fair_value_risk_free_rate_bps),
        settlement_cost_bps=(settings.fund_csp_sepolia_fair_value_settlement_cost_bps),
    )


def get_fund_covered_call_sepolia_observer_private_keys() -> tuple[str, ...]:
    """Return dedicated keys for the versioned covered-call fair-value policy."""
    from eth_account import Account

    keys = tuple(
        key.strip()
        for key in settings.fund_covered_call_sepolia_observer_private_keys.split(",")
        if key.strip()
    )
    if len(keys) != 2:
        raise ValueError(
            "FUND_COVERED_CALL_SEPOLIA_OBSERVER_PRIVATE_KEYS must contain "
            "exactly two keys"
        )
    addresses = []
    for key in keys:
        try:
            addresses.append(Account.from_key(key).address.lower())
        except Exception as exc:
            raise ValueError(
                "Invalid FUND_COVERED_CALL_SEPOLIA_OBSERVER_PRIVATE_KEYS entry"
            ) from exc
    if len(addresses) != len(set(addresses)):
        raise ValueError(
            "FUND_COVERED_CALL_SEPOLIA_OBSERVER_PRIVATE_KEYS contains "
            "duplicate observers"
        )
    return keys


def get_fund_covered_call_sepolia_fair_value_policy():
    """Return the explicit Base Sepolia European-call fair-value policy."""
    from src.fund_nav.fair_value import (
        CALL_POLICY_IV_BPS,
        CALL_POLICY_IV_SOURCE,
        CALL_POLICY_RISK_FREE_RATE_BPS,
        CALL_POLICY_SETTLEMENT_COST_BPS,
        CoveredCallFairValuePolicy,
    )

    policy = CoveredCallFairValuePolicy(
        implied_volatility_bps=(settings.fund_covered_call_sepolia_fair_value_iv_bps),
        implied_volatility_source=(
            settings.fund_covered_call_sepolia_fair_value_iv_source
        ),
        risk_free_rate_bps=(
            settings.fund_covered_call_sepolia_fair_value_risk_free_rate_bps
        ),
        settlement_cost_bps=(
            settings.fund_covered_call_sepolia_fair_value_settlement_cost_bps
        ),
    )
    if (
        policy.implied_volatility_bps != CALL_POLICY_IV_BPS
        or policy.implied_volatility_source != CALL_POLICY_IV_SOURCE
        or policy.risk_free_rate_bps != CALL_POLICY_RISK_FREE_RATE_BPS
        or policy.settlement_cost_bps != CALL_POLICY_SETTLEMENT_COST_BPS
    ):
        raise ValueError(
            "FUND_COVERED_CALL_SEPOLIA fair-value inputs must match the "
            "approved B1N-358 policy"
        )
    return policy


@functools.lru_cache(maxsize=None)
def _parse_allowlist(raw: str) -> set[str]:
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _default_tradable_assets() -> str:
    if settings.app_env.lower() == "production":
        return "eth,btc"
    return "eth,btc,sol,tslax"


def _default_tradable_chains() -> str:
    if settings.app_env.lower() == "production":
        return "base"
    return "base,solana"


def get_tradable_assets_allowlist() -> set[str]:
    return _parse_allowlist(settings.tradable_assets or _default_tradable_assets())


def get_tradable_chains_allowlist() -> set[str]:
    return _parse_allowlist(settings.tradable_chains or _default_tradable_chains())


def is_asset_visible(asset: str) -> bool:
    return asset.lower() in _parse_allowlist(settings.visible_assets)


def is_asset_tradable(asset: str) -> bool:
    return asset.lower() in get_tradable_assets_allowlist()


def is_chain_tradable(chain: str) -> bool:
    return chain.lower() in get_tradable_chains_allowlist()


def get_cctp_attestation_url() -> str:
    """Return the Circle attestation API URL, defaulting by beta_mode."""
    if settings.cctp_attestation_api_url:
        return settings.cctp_attestation_api_url
    if settings.beta_mode:
        return "https://iris-api-sandbox.circle.com"
    return "https://iris-api.circle.com"


def has_bridge_config() -> bool:
    """True when CCTP relayer wallets + contracts are configured."""
    has_base_message_transmitter = (
        bool(settings.cctp_base_message_transmitter) or settings.chain_id == 8453
    )
    has_relayer_key = bool(
        settings.relayer_base_private_key
        or settings.operator_private_key
        or settings.relayer_solana_keypair
    )
    return bool(has_base_message_transmitter and has_relayer_key)


def has_solana_config() -> bool:
    """True when Solana RPC + operator + core programs are configured."""
    return bool(
        settings.solana_rpc_url
        and settings.solana_operator_keypair
        and settings.solana_batch_settler_program_id
        and settings.solana_otoken_factory_program_id
    )


def _solana_bots_default_enabled() -> bool:
    """Enable Solana runtime by default outside production."""
    return settings.app_env.lower() != "production"


def has_solana_runtime_enabled() -> bool:
    """True when Solana background runtime is enabled for this environment."""
    if settings.solana_bots_enabled is not None:
        return settings.solana_bots_enabled
    return _solana_bots_default_enabled()


def is_solana_bot_enabled(bot_name: str) -> bool:
    """Return whether an individual Solana bot should run."""
    overrides = {
        "circuit_breaker": settings.solana_circuit_breaker_bot_enabled,
        "event_indexer": settings.solana_event_indexer_enabled,
        "expiry_settler": settings.solana_expiry_settler_enabled,
        "otoken_manager": settings.solana_otoken_manager_enabled,
    }
    if bot_name not in overrides:
        raise ValueError(f"Unknown Solana bot: {bot_name}")

    override = overrides[bot_name]
    if override is not None:
        return override
    return has_solana_runtime_enabled()


def has_enabled_solana_bots() -> bool:
    """True when at least one Solana bot is enabled after applying overrides."""
    return any(
        is_solana_bot_enabled(bot_name)
        for bot_name in (
            "circuit_breaker",
            "event_indexer",
            "expiry_settler",
            "otoken_manager",
        )
    )
