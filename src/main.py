import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.routes import router
from src.api.analytics import router as analytics_router
from src.api.mm_routes import router as mm_router
from src.api.mm_ws import router as mm_ws_router
from src.api.activity import router as activity_router
from src.api.notifications import router as notifications_router
from src.api.b1nary_accounts import router as b1nary_accounts_router
from src.api.yield_routes import router as yield_router
from src.api.csp_vault import router as csp_vault_router
from src.api.series import router as series_router
from src.api.fund_series import router as fund_series_router
from src.bridge.routes import router as bridge_router
from src.config import (
    get_tokenized_fund_rpc_url,
    get_fund_covered_call_sepolia_fair_value_policy,
    get_fund_covered_call_sepolia_observer_private_keys,
    get_fund_csp_sepolia_fair_value_policy,
    get_fund_csp_sepolia_observer_private_keys,
    get_fund_nav_reporter_private_keys,
    get_fund_nav_submitter_private_key,
    settings,
    has_solana_config,
    has_bridge_config,
    validate_backend_rpc_config,
    validate_meta_wheel_credential_topology,
)

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class _RpcEndpointRedactingFormatter(logging.Formatter):
    def __init__(self, *args, endpoints: tuple[str | None, ...], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        sensitive_parts: list[str] = []
        for endpoint in endpoints:
            if not endpoint:
                continue
            parsed = urlsplit(endpoint)
            sensitive_parts.append(endpoint)
            if parsed.netloc:
                sensitive_parts.append(parsed.netloc)
            if parsed.hostname:
                sensitive_parts.append(parsed.hostname)
            if parsed.path not in ("", "/"):
                sensitive_parts.append(parsed.path)
            if parsed.query:
                sensitive_parts.append(f"?{parsed.query}")
        self.sensitive_parts = tuple(
            sorted(set(sensitive_parts), key=len, reverse=True)
        )

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for endpoint in self.sensitive_parts:
            rendered = rendered.replace(endpoint, "[REDACTED_RPC_ENDPOINT]")
        return rendered


logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
_formatter = _RpcEndpointRedactingFormatter(
    _LOG_FORMAT,
    endpoints=(
        settings.rpc_url,
        settings.wss_rpc_url,
        settings.tokenized_fund_rpc_url,
        settings.solana_rpc_url,
        settings.solana_wss_rpc_url,
    ),
)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_formatter)

logger = logging.getLogger(__name__)

SUPPORTED_LAZY_OTOKEN_CHAIN_IDS = {8453, 84532}
_LAZY_BASE_ASSET_ADDRESSES = {
    "eth": ("WETH_ADDRESS", "weth_address"),
    "btc": ("WBTC_ADDRESS", "wbtc_address"),
}


def validate_fund_runtime_cadences() -> None:
    """Reject enabled fund loops configured below operationally safe minima."""
    if (
        settings.tokenized_fund_indexer_enabled
        and settings.tokenized_fund_indexer_poll_interval_seconds < 5
    ):
        raise RuntimeError(
            "TOKENIZED_FUND_INDEXER_POLL_INTERVAL_SECONDS must be at least 5 "
            "when the indexer is enabled"
        )
    if (
        settings.fund_nav_reporter_enabled
        and settings.fund_nav_reporter_interval_seconds < 15
    ):
        raise RuntimeError(
            "FUND_NAV_REPORTER_INTERVAL_SECONDS must be at least 15 when the "
            "NAV reporter is enabled"
        )


def _configured_lazy_assets() -> set[str]:
    return {
        item.strip().lower()
        for item in settings.otoken_lazy_assets.split(",")
        if item.strip()
    }


def validate_lazy_otoken_config(series_mode: str) -> None:
    """Fail fast only for lazy mode; eager remains the rollback-safe default."""
    if series_mode != "lazy":
        return
    lazy_assets = _configured_lazy_assets()
    if not lazy_assets:
        raise RuntimeError("OTOKEN_LAZY_ASSETS must contain at least one Base asset")
    unsupported_assets = lazy_assets - set(_LAZY_BASE_ASSET_ADDRESSES)
    if unsupported_assets:
        raise RuntimeError(
            "OTOKEN_LAZY_ASSETS contains unsupported Base assets: "
            + ", ".join(sorted(unsupported_assets))
        )
    required = {
        "RPC_URL": settings.rpc_url,
        "BATCH_SETTLER_ADDRESS": settings.batch_settler_address,
        "OTOKEN_FACTORY_ADDRESS": settings.otoken_factory_address,
        "WHITELIST_ADDRESS": settings.whitelist_address,
        "OPERATOR_PRIVATE_KEY": settings.operator_private_key,
        "USDC_ADDRESS": settings.usdc_address,
        "OTOKEN_INTENT_HMAC_SECRET": settings.otoken_intent_hmac_secret,
        "PRIVY_APP_ID": settings.privy_app_id,
        "PRIVY_APP_SECRET": settings.privy_app_secret,
    }
    for asset in lazy_assets:
        env_name, setting_name = _LAZY_BASE_ASSET_ADDRESSES[asset]
        required[env_name] = getattr(settings, setting_name)
    missing = [
        name
        for name, value in required.items()
        if not value or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise RuntimeError(
            "Lazy oToken mode is missing required configuration: " + ", ".join(missing)
        )
    if not (
        settings.privy_jwt_verification_key.strip() or settings.privy_jwks_url.strip()
    ):
        raise RuntimeError(
            "Lazy oToken mode requires at least one of "
            "PRIVY_JWT_VERIFICATION_KEY or PRIVY_JWKS_URL"
        )
    if settings.privy_jwks_url.strip():
        from src.api.user_auth import validate_privy_jwks_url

        try:
            validate_privy_jwks_url(settings.privy_jwks_url)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
    if settings.chain_id not in SUPPORTED_LAZY_OTOKEN_CHAIN_IDS:
        raise RuntimeError(
            "Lazy oToken mode only supports Base mainnet (8453) or Base Sepolia (84532)"
        )
    materialization_buffer = settings.otoken_materialization_deadline_buffer_seconds
    execution_buffer = settings.otoken_ensure_deadline_buffer_seconds
    if materialization_buffer <= 120 or materialization_buffer < execution_buffer + 120:
        raise RuntimeError(
            "OTOKEN_MATERIALIZATION_DEADLINE_BUFFER_SECONDS must exceed "
            "120 seconds and cover the execution buffer plus the 120-second "
            "transaction timeout"
        )


def _lazy_contract_addresses():
    """Return named, checksummed addresses without exposing values in errors."""
    from web3 import Web3

    configured = {
        "BATCH_SETTLER_ADDRESS": settings.batch_settler_address,
        "OTOKEN_FACTORY_ADDRESS": settings.otoken_factory_address,
        "WHITELIST_ADDRESS": settings.whitelist_address,
        "USDC_ADDRESS": settings.usdc_address,
    }
    for asset in _configured_lazy_assets():
        env_name, setting_name = _LAZY_BASE_ASSET_ADDRESSES[asset]
        configured[env_name] = getattr(settings, setting_name)

    checksummed = {}
    for name, value in configured.items():
        try:
            address = Web3.to_checksum_address(value)
        except (TypeError, ValueError):
            raise RuntimeError(f"{name} must be a valid EVM address") from None
        if address == "0x0000000000000000000000000000000000000000":
            raise RuntimeError(f"{name} must not be the zero address")
        checksummed[name] = address

    by_address: dict[str, list[str]] = {}
    for name, address in checksummed.items():
        by_address.setdefault(address.lower(), []).append(name)
    duplicates = [names for names in by_address.values() if len(names) > 1]
    if duplicates:
        names = ", ".join(sorted(duplicates[0]))
        raise RuntimeError(f"Lazy oToken configuration reuses one address for {names}")
    return checksummed


def _has_contract_code(code) -> bool:
    if isinstance(code, str):
        normalized = code.removeprefix("0x")
        return bool(normalized) and any(char != "0" for char in normalized)
    raw = bytes(code)
    return bool(raw) and any(raw)


def validate_lazy_otoken_chain_config(series_mode: str, w3=None) -> None:
    """Verify lazy-mode Base deployment wiring with read-only RPC calls."""
    if series_mode != "lazy":
        return
    validate_lazy_otoken_config(series_mode)

    from src.contracts.abis import (
        ADDRESS_BOOK_ABI,
        BATCH_SETTLER_ABI,
        OTOKEN_FACTORY_ABI,
    )
    from src.contracts.web3_client import get_operator_account, get_w3

    addresses = _lazy_contract_addresses()
    try:
        expected_operator = get_operator_account().address
    except (TypeError, ValueError):
        raise RuntimeError("OPERATOR_PRIVATE_KEY must be a valid private key") from None

    if w3 is None:
        try:
            w3 = get_w3()
        except Exception:
            raise RuntimeError(
                "Lazy oToken RPC preflight could not initialize RPC_URL"
            ) from None
    try:
        connected = w3.is_connected()
    except Exception:
        raise RuntimeError(
            "Lazy oToken RPC preflight could not connect to RPC_URL"
        ) from None
    if not connected:
        raise RuntimeError("Lazy oToken RPC preflight could not connect to RPC_URL")
    try:
        rpc_chain_id = int(w3.eth.chain_id)
    except Exception:
        raise RuntimeError(
            "Lazy oToken RPC preflight could not read the chain ID"
        ) from None
    if rpc_chain_id != settings.chain_id:
        raise RuntimeError(
            "Lazy oToken RPC chain mismatch: CHAIN_ID does not match RPC_URL"
        )

    for name, address in addresses.items():
        try:
            code = w3.eth.get_code(address)
        except Exception:
            raise RuntimeError(
                f"Lazy oToken RPC preflight could not read code for {name}"
            ) from None
        if not _has_contract_code(code):
            raise RuntimeError(f"{name} has no deployed bytecode on configured chain")

    factory = w3.eth.contract(
        address=addresses["OTOKEN_FACTORY_ADDRESS"],
        abi=OTOKEN_FACTORY_ABI,
    )
    batch_settler = w3.eth.contract(
        address=addresses["BATCH_SETTLER_ADDRESS"],
        abi=BATCH_SETTLER_ABI,
    )
    try:
        factory_address_book = factory.functions.addressBook().call()
        batch_address_book = batch_settler.functions.addressBook().call()
        configured_operator = factory.functions.operator().call()
    except Exception:
        raise RuntimeError(
            "Lazy oToken preflight could not read factory/settler configuration"
        ) from None

    if factory_address_book.lower() != batch_address_book.lower():
        raise RuntimeError(
            "Lazy oToken crossed configuration: factory and settler use "
            "different AddressBook contracts"
        )
    if configured_operator.lower() != expected_operator.lower():
        raise RuntimeError(
            "Lazy oToken crossed configuration: OPERATOR_PRIVATE_KEY does not "
            "match factory operator"
        )

    try:
        address_book_code = w3.eth.get_code(factory_address_book)
    except Exception:
        raise RuntimeError(
            "Lazy oToken RPC preflight could not read AddressBook code"
        ) from None
    if not _has_contract_code(address_book_code):
        raise RuntimeError("Factory AddressBook has no deployed bytecode")

    address_book = w3.eth.contract(
        address=factory_address_book,
        abi=ADDRESS_BOOK_ABI,
    )
    try:
        address_book_factory = address_book.functions.oTokenFactory().call()
        address_book_whitelist = address_book.functions.whitelist().call()
        address_book_settler = address_book.functions.batchSettler().call()
    except Exception:
        raise RuntimeError(
            "Lazy oToken preflight could not read AddressBook configuration"
        ) from None
    expected_wiring = {
        "OTOKEN_FACTORY_ADDRESS": (
            address_book_factory,
            addresses["OTOKEN_FACTORY_ADDRESS"],
        ),
        "WHITELIST_ADDRESS": (
            address_book_whitelist,
            addresses["WHITELIST_ADDRESS"],
        ),
        "BATCH_SETTLER_ADDRESS": (
            address_book_settler,
            addresses["BATCH_SETTLER_ADDRESS"],
        ),
    }
    crossed = [
        name
        for name, (observed, expected) in expected_wiring.items()
        if observed.lower() != expected.lower()
    ]
    if crossed:
        raise RuntimeError(
            "Lazy oToken crossed configuration in AddressBook: " + ", ".join(crossed)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background bots when contract addresses are configured."""
    # Safety invariants — checked before any background tasks are spawned so
    # that a misconfigured production server fails fast with no orphaned tasks.
    if settings.allowed_origins.strip() == "*":
        if not settings.beta_mode:
            logger.critical(
                "STARTUP ABORTED: CORS is '*' in production mode. "
                "Set ALLOWED_ORIGINS to your production domain(s)."
            )
            raise RuntimeError(
                "CORS cannot be '*' in production mode. "
                "Set ALLOWED_ORIGINS to your production domain(s)."
            )
        logger.warning(
            "CORS is configured to allow all origins ('*'). "
            "Set ALLOWED_ORIGINS to your production domain(s) before deploying to mainnet."
        )

    try:
        validate_backend_rpc_config()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None

    if not settings.background_workers_enabled:
        logger.warning(
            "Background workers disabled by BACKGROUND_WORKERS_ENABLED=false; "
            "API routes and health checks remain active"
        )
        yield
        return

    validate_fund_runtime_cadences()
    if (
        settings.rpc_snapshot_collector_enabled
        and settings.tokenized_fund_indexer_enabled
    ):
        raise RuntimeError(
            "RPC snapshot collector and legacy tokenized fund indexer cannot run together"
        )
    if settings.tokenized_fund_indexer_enabled and not get_tokenized_fund_rpc_url():
        raise RuntimeError(
            "TOKENIZED_FUND_RPC_URL or RPC_URL is required when "
            "TOKENIZED_FUND_INDEXER_ENABLED=true"
        )
    if settings.rpc_snapshot_collector_enabled and not get_tokenized_fund_rpc_url():
        raise RuntimeError(
            "TOKENIZED_FUND_RPC_URL or RPC_URL is required when "
            "RPC_SNAPSHOT_COLLECTOR_ENABLED=true"
        )
    if settings.fund_nav_reporter_enabled and not get_tokenized_fund_rpc_url():
        raise RuntimeError(
            "TOKENIZED_FUND_RPC_URL or RPC_URL is required when "
            "FUND_NAV_REPORTER_ENABLED=true"
        )
    wheel_reporter_configured = bool(settings.meta_wheel_fund_key.strip())
    standalone_reporter_configured = bool(
        settings.fund_nav_reporter_private_keys.strip()
        or settings.fund_nav_submitter_private_key.strip()
    )
    standalone_observations_enabled = bool(
        settings.fund_csp_sepolia_fair_value_observations_enabled
        or settings.fund_covered_call_sepolia_fair_value_observations_enabled
    )
    wheel_observations_enabled = bool(
        settings.meta_wheel_csp_sepolia_fair_value_observations_enabled
        or settings.meta_wheel_covered_call_sepolia_fair_value_observations_enabled
    )
    if settings.fund_nav_reporter_enabled and not (
        wheel_reporter_configured or standalone_reporter_configured
    ):
        raise RuntimeError(
            "FUND_NAV_REPORTER_PRIVATE_KEYS or Meta Wheel credentials are required when "
            "FUND_NAV_REPORTER_ENABLED=true"
        )
    if settings.fund_nav_reporter_enabled:
        if not 15 <= settings.fund_nav_reporter_lease_seconds <= 900:
            raise RuntimeError(
                "FUND_NAV_REPORTER_LEASE_SECONDS must be between 15 and 900"
            )
        from eth_account import Account

        reporter_addresses = set()
        submitter_address = None
        if standalone_reporter_configured or standalone_observations_enabled:
            try:
                reporter_keys = get_fund_nav_reporter_private_keys()
                submitter_key = get_fund_nav_submitter_private_key()
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            reporter_addresses = {
                Account.from_key(key).address.lower() for key in reporter_keys
            }
            submitter_address = Account.from_key(submitter_key).address.lower()
            if submitter_address in reporter_addresses:
                raise RuntimeError(
                    "NAV submitter key must be separate from NAV reporter keys"
                )
        fair_value_configs = (
            (
                "CSP",
                settings.fund_csp_sepolia_fair_value_observations_enabled,
                get_fund_csp_sepolia_observer_private_keys,
                get_fund_csp_sepolia_fair_value_policy,
            ),
            (
                "covered-call",
                settings.fund_covered_call_sepolia_fair_value_observations_enabled,
                get_fund_covered_call_sepolia_observer_private_keys,
                get_fund_covered_call_sepolia_fair_value_policy,
            ),
        )
        observer_sets = {}
        for label, enabled, get_keys, get_policy in fair_value_configs:
            if not enabled:
                continue
            if settings.chain_id != 84532:
                raise RuntimeError(
                    f"Fair-value {label} observations are restricted to Base Sepolia"
                )
            try:
                observer_keys = get_keys()
                get_policy()
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            observer_addresses = {
                Account.from_key(key).address.lower() for key in observer_keys
            }
            if reporter_addresses & observer_addresses:
                raise RuntimeError(
                    f"Sepolia {label} observer keys must be separate from "
                    "NAV reporter keys"
                )
            if (
                submitter_address is not None
                and submitter_address in observer_addresses
            ):
                raise RuntimeError(
                    f"NAV submitter key must be separate from {label} observer keys"
                )
            observer_sets[label] = observer_addresses
        if observer_sets.get("CSP", set()) & observer_sets.get("covered-call", set()):
            raise RuntimeError("CSP and covered-call observer keys must be separate")
        if wheel_reporter_configured:
            try:
                validate_meta_wheel_credential_topology()
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
    elif standalone_observations_enabled or wheel_observations_enabled:
        raise RuntimeError(
            "FUND_NAV_REPORTER_ENABLED is required for fair-value observations"
        )

    tasks = []

    configured_series_mode = settings.otoken_series_mode.strip().lower()
    if settings.base_otoken_manager_enabled:
        validate_lazy_otoken_config(configured_series_mode)
        validate_lazy_otoken_chain_config(configured_series_mode)

    has_on_chain_config = (
        settings.batch_settler_address
        and settings.operator_private_key
        and settings.otoken_factory_address
    )
    requested_base_workers = any(
        (
            settings.base_otoken_manager_enabled,
            settings.base_event_indexer_enabled,
            settings.base_expiry_settler_enabled,
            settings.base_circuit_breaker_enabled,
        )
    )
    if has_on_chain_config:
        started_base_workers = []
        if settings.base_otoken_manager_enabled:
            from src.bots import otoken_manager
            from src.bots.otoken_manager import get_otoken_series_mode

            get_otoken_series_mode()
            tasks.append(asyncio.create_task(otoken_manager.run()))
            started_base_workers.append("oToken manager")
        if settings.base_event_indexer_enabled:
            from src.bots import event_indexer

            tasks.append(asyncio.create_task(event_indexer.run()))
            started_base_workers.append("event indexer")
        if settings.base_expiry_settler_enabled:
            from src.bots import expiry_settler

            tasks.append(asyncio.create_task(expiry_settler.run()))
            started_base_workers.append("expiry settler")
        if settings.base_circuit_breaker_enabled:
            from src.bots import circuit_breaker_bot

            tasks.append(asyncio.create_task(circuit_breaker_bot.run()))
            started_base_workers.append("circuit breaker")
        if started_base_workers:
            logger.info("Base workers started: %s", ", ".join(started_base_workers))
    elif requested_base_workers:
        logger.info(
            "Base workers not started: contract addresses or operator key not configured"
        )

    if settings.tokenized_fund_indexer_enabled:
        from src.fund_indexer import indexer as fund_indexer

        tasks.append(asyncio.create_task(fund_indexer.run()))
        logger.info("Tokenized fund indexer started")

    if settings.rpc_snapshot_collector_enabled:
        from src.fund_indexer import collector as snapshot_collector

        snapshot_rpc = await asyncio.to_thread(
            snapshot_collector.create_validated_rpc,
            settings.app_env,
            settings.chain_id,
        )
        tasks.append(asyncio.create_task(snapshot_collector.run(snapshot_rpc)))
        logger.info("RPC snapshot collector started")

    if settings.fund_nav_reporter_enabled:
        from src.bots import fund_nav_reporter

        tasks.append(asyncio.create_task(fund_nav_reporter.run()))
        logger.info("Fund NAV reporter started")

    # Yield indexer needs controller + margin pool addresses
    if (
        settings.yield_indexer_enabled
        and settings.controller_address
        and settings.margin_pool_address
    ):
        from src.bots import yield_indexer

        tasks.append(asyncio.create_task(yield_indexer.run()))
        logger.info("Yield indexer started")

    if settings.legacy_agora_v1_enabled:
        # Import only inside the rollback gate. The default v2 process must not
        # load or schedule the global legacy snapshot implementation.
        from src.bots import weekly_aggregator

        tasks.append(asyncio.create_task(weekly_aggregator.run()))
        logger.info("Legacy Agora v1 weekly aggregator started")

    logger.info("Solana background workers disabled: Base-only runtime")

    # ── Bridge relayer ──
    if settings.bridge_relayer_enabled:
        if has_bridge_config():
            from src.bridge import relayer as bridge_relayer

            tasks.append(asyncio.create_task(bridge_relayer.run()))
            logger.info("Bridge relayer started")
        else:
            logger.info(
                "Bridge relayer not started: CCTP addresses or relayer keys not configured"
            )

    # Notification bot only needs Resend API key, not on-chain config
    if settings.notification_bot_enabled:
        if settings.resend_api_key:
            from src.bots import notification_bot

            tasks.append(asyncio.create_task(notification_bot.run()))
            logger.info("Notification bot started")
        else:
            logger.info("Notification bot not started: RESEND_API_KEY not configured")

    yield

    for task in tasks:
        task.cancel()


openapi_tags = [
    {
        "name": "Market Data",
        "description": "Live ETH option prices from all market makers. Picks the best bid per oToken and includes EIP-712 signature data for on-chain execution.",
    },
    {
        "name": "Market Making",
        "description": "MM quote management: submit, retrieve, and cancel EIP-712 signed quotes. Requires X-API-Key header.",
    },
    {
        "name": "MM Monitoring",
        "description": "MM monitoring: fills, open positions, risk exposure, market data, and real-time WebSocket notifications. Requires X-API-Key header.",
    },
    {
        "name": "Positions",
        "description": "Query open and settled option positions for a given wallet address. Data is indexed from on-chain OrderExecuted events.",
    },
    {
        "name": "Simulation",
        "description": "Back-test selling a cash-secured put over the last 7 days of real ETH price history.",
    },
    {
        "name": "Results",
        "description": "Weekly performance reports and cumulative user statistics.",
    },
    {
        "name": "Waitlist",
        "description": "Join the b1nary waitlist. Idempotent — duplicate emails return 200.",
    },
    {
        "name": "Analytics",
        "description": "Fire-and-forget event logging for frontend interactions (slider usage, engagement events).",
    },
    {
        "name": "Leaderboard",
        "description": "Earnings Challenge leaderboard — two tracks per wallet.",
    },
    {
        "name": "Yield",
        "description": "Aave yield tracking, distributions, and per-user stats.",
    },
    {
        "name": "Notifications",
        "description": "Email notification opt-in, verification, and unsubscribe.",
    },
    {
        "name": "Bridge",
        "description": "CCTP V2 cross-chain USDC bridging and trade execution. Orchestrates burn→attestation→mint→trade.",
    },
    {
        "name": "B1nary Accounts",
        "description": "Product account identity, Privy user membership, and verified wallet linking.",
    },
    {
        "name": "System",
        "description": "Health checks and operational status.",
    },
]

app = FastAPI(
    title="b1nary API",
    summary="Simplified ETH options protocol on Base",
    description=(
        "b1nary lets users sell cash-secured puts and covered calls on ETH and earn premium. "
        "A market maker is the counterparty; settlement is instant and on-chain.\n\n"
        "## Quick start for AI agents\n\n"
        "1. `GET /prices` — fetch the current option price menu (best bid across all MMs)\n"
        "2. Pick a quote and call `executeOrder()` on the BatchSettler contract with the included signature\n"
        "3. `GET /positions/{address}` — check the user's open and settled positions\n\n"
        "All monetary values are in USD unless noted. On-chain amounts use the token's native decimals "
        "(oToken = 8, USDC = 6, WETH = 18).\n\n"
        f"**Chain:** Base (chain ID {settings.chain_id})  \n"
        "**Contracts:** see [BaseScan](https://basescan.org)"
    ),
    version="0.4.0",
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Portfolio-Bounded",
        "X-Portfolio-Watermark",
        "X-Active-Limit",
        "X-Active-Has-More",
        "X-Active-Next-Cursor",
        "X-Settled-Limit",
        "X-Settled-Has-More",
        "X-Settled-Next-Cursor",
    ],
)

app.include_router(router)
app.include_router(series_router)
app.include_router(fund_series_router)
app.include_router(analytics_router)
app.include_router(mm_router)
app.include_router(mm_ws_router)
app.include_router(activity_router)
app.include_router(notifications_router)
app.include_router(b1nary_accounts_router)
app.include_router(yield_router)
app.include_router(csp_vault_router)
app.include_router(bridge_router)

if settings.legacy_agora_v1_enabled:
    # Keep the retired API available as an explicit, reversible rollback path
    # without importing it into the default v2 process.
    from src.api.leaderboard import router as leaderboard_router
    from src.api.results import router as results_router

    app.include_router(results_router)
    app.include_router(leaderboard_router)
    logger.warning("Legacy Agora v1 API routes enabled")

if settings.beta_mode:
    from src.api.demo import router as demo_router
    from src.api.faucet import router as faucet_router

    app.include_router(demo_router)
    app.include_router(faucet_router)

    if has_solana_config() and settings.solana_usdc_mint:
        from src.api.solana_faucet import router as solana_faucet_router

        app.include_router(solana_faucet_router)
        logger.info("Solana faucet enabled at /faucet/solana")

    app.openapi_tags = (app.openapi_tags or []) + [  # type: ignore[operator]
        {
            "name": "Faucet",
            "description": "Send gas ETH + test tokens on testnet. 1 claim per wallet (permanent). Beta only — disabled in production.",
        },
        {
            "name": "Demo",
            "description": "Beta-only endpoints for triggering instant settlement in testnet. Requires X-Demo-Key header. Disabled in production.",
        },
    ]
    app.openapi_schema = None  # invalidate cached schema so tag mutation takes effect
    logger.info("Beta mode: /demo/settle and /faucet endpoints enabled")


@app.get("/health", tags=["System"], summary="Health check")
async def health():
    """Returns `{\"status\": \"ok\"}` when the API is running."""
    return {"status": "ok"}


@app.get(
    "/health/snapshot-collector",
    tags=["System"],
    summary="Snapshot collector health",
)
async def snapshot_collector_health():
    if not settings.rpc_snapshot_collector_enabled:
        return {"status": "disabled"}
    try:
        from src.db.database import get_client
        from src.fund_indexer.collector import snapshot_collector_startup_healthy

        if not snapshot_collector_startup_healthy(settings.app_env, settings.chain_id):
            raise RuntimeError("Snapshot collector startup validation unavailable")
        result = (
            get_client()
            .rpc(
                "v2_get_current_snapshot",
                {"p_environment": settings.app_env, "p_chain_id": settings.chain_id},
            )
            .execute()
        )
        payload = result.data
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        healthy = bool(
            payload and payload.get("reconciled") and not payload.get("stale", True)
        )
    except Exception:
        healthy = False
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy"},
    )
