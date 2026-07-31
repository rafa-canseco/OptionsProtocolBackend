import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
from src.api.results import router as results_router
from src.api.analytics import router as analytics_router
from src.api.mm_routes import router as mm_router
from src.api.mm_ws import router as mm_ws_router
from src.api.activity import router as activity_router
from src.api.leaderboard import router as leaderboard_router
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
    has_enabled_solana_bots,
    is_solana_bot_enabled,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SUPPORTED_LAZY_OTOKEN_CHAIN_IDS = {8453, 84532}
_LAZY_BASE_ASSET_ADDRESSES = {
    "eth": ("WETH_ADDRESS", "weth_address"),
    "btc": ("WBTC_ADDRESS", "wbtc_address"),
}


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

    if settings.tokenized_fund_indexer_enabled and not get_tokenized_fund_rpc_url():
        raise RuntimeError(
            "TOKENIZED_FUND_RPC_URL or RPC_URL is required when "
            "TOKENIZED_FUND_INDEXER_ENABLED=true"
        )
    if settings.fund_nav_reporter_enabled and not get_tokenized_fund_rpc_url():
        raise RuntimeError(
            "TOKENIZED_FUND_RPC_URL or RPC_URL is required when "
            "FUND_NAV_REPORTER_ENABLED=true"
        )
    if (
        settings.fund_nav_reporter_enabled
        and not settings.fund_nav_reporter_private_keys
    ):
        raise RuntimeError(
            "FUND_NAV_REPORTER_PRIVATE_KEYS is required when "
            "FUND_NAV_REPORTER_ENABLED=true"
        )
    if settings.fund_nav_reporter_enabled:
        if not 15 <= settings.fund_nav_reporter_lease_seconds <= 900:
            raise RuntimeError(
                "FUND_NAV_REPORTER_LEASE_SECONDS must be between 15 and 900"
            )
        try:
            reporter_keys = get_fund_nav_reporter_private_keys()
            submitter_key = get_fund_nav_submitter_private_key()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        from eth_account import Account

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
            if submitter_address in observer_addresses:
                raise RuntimeError(
                    f"NAV submitter key must be separate from {label} observer keys"
                )
            observer_sets[label] = observer_addresses
        if observer_sets.get("CSP", set()) & observer_sets.get("covered-call", set()):
            raise RuntimeError("CSP and covered-call observer keys must be separate")
    elif (
        settings.fund_csp_sepolia_fair_value_observations_enabled
        or settings.fund_covered_call_sepolia_fair_value_observations_enabled
    ):
        raise RuntimeError(
            "FUND_NAV_REPORTER_ENABLED is required for fair-value observations"
        )

    tasks = []

    configured_series_mode = settings.otoken_series_mode.strip().lower()
    validate_lazy_otoken_config(configured_series_mode)
    validate_lazy_otoken_chain_config(configured_series_mode)

    has_on_chain_config = (
        settings.batch_settler_address
        and settings.operator_private_key
        and settings.otoken_factory_address
    )
    if has_on_chain_config:
        from src.bots.otoken_manager import get_otoken_series_mode

        get_otoken_series_mode()
        from src.bots import (
            otoken_manager,
            event_indexer,
            expiry_settler,
            circuit_breaker_bot,
        )

        tasks.append(asyncio.create_task(otoken_manager.run()))
        tasks.append(asyncio.create_task(event_indexer.run()))
        tasks.append(asyncio.create_task(expiry_settler.run()))
        tasks.append(asyncio.create_task(circuit_breaker_bot.run()))
        logger.info("Started %d on-chain bots", len(tasks))
    else:
        logger.info(
            "On-chain bots not started: contract addresses or operator key not configured"
        )

    if settings.tokenized_fund_indexer_enabled:
        from src.fund_indexer import indexer as fund_indexer

        tasks.append(asyncio.create_task(fund_indexer.run()))
        logger.info("Tokenized fund indexer started")

    if settings.fund_nav_reporter_enabled:
        from src.bots import fund_nav_reporter

        tasks.append(asyncio.create_task(fund_nav_reporter.run()))
        logger.info("Fund NAV reporter started")

    # Yield indexer needs controller + margin pool addresses
    if settings.controller_address and settings.margin_pool_address:
        from src.bots import yield_indexer

        tasks.append(asyncio.create_task(yield_indexer.run()))
        logger.info("Yield indexer started")

    # Weekly aggregator only needs DB access, not on-chain config
    from src.bots import weekly_aggregator

    tasks.append(asyncio.create_task(weekly_aggregator.run()))
    logger.info("Weekly aggregator started")

    # ── Solana bots ──
    if has_solana_config() and has_enabled_solana_bots():
        from src.bots import (
            solana_circuit_breaker_bot,
            solana_event_indexer,
            solana_expiry_settler,
            solana_otoken_manager,
        )

        started_solana_bots = []
        if is_solana_bot_enabled("circuit_breaker"):
            tasks.append(asyncio.create_task(solana_circuit_breaker_bot.run()))
            started_solana_bots.append("circuit breaker")
        if is_solana_bot_enabled("event_indexer"):
            tasks.append(asyncio.create_task(solana_event_indexer.run()))
            started_solana_bots.append("event indexer")
        if is_solana_bot_enabled("expiry_settler"):
            tasks.append(asyncio.create_task(solana_expiry_settler.run()))
            started_solana_bots.append("expiry settler")
        if is_solana_bot_enabled("otoken_manager"):
            tasks.append(asyncio.create_task(solana_otoken_manager.run()))
            started_solana_bots.append("otoken manager")

        if started_solana_bots:
            logger.info(
                "Solana bots started (cluster=%s): %s",
                settings.solana_cluster,
                ", ".join(started_solana_bots),
            )
        else:
            logger.info("Solana runtime enabled but no Solana bots selected by flags")
    elif has_solana_config():
        logger.info(
            "Solana bots not started: runtime disabled for env=%s (set SOLANA_BOTS_ENABLED=true or enable an individual bot flag to opt in)",
            settings.app_env,
        )
    else:
        logger.info(
            "Solana bots not started: SOLANA_RPC_URL or program IDs not configured"
        )

    # ── Bridge relayer ──
    if has_bridge_config():
        from src.bridge import relayer as bridge_relayer

        tasks.append(asyncio.create_task(bridge_relayer.run()))
        logger.info("Bridge relayer started")
    else:
        logger.info(
            "Bridge relayer not started: CCTP addresses or relayer keys not configured"
        )

    # Notification bot only needs Resend API key, not on-chain config
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
)

app.include_router(router)
app.include_router(series_router)
app.include_router(fund_series_router)
app.include_router(results_router)
app.include_router(analytics_router)
app.include_router(mm_router)
app.include_router(mm_ws_router)
app.include_router(activity_router)
app.include_router(leaderboard_router)
app.include_router(notifications_router)
app.include_router(b1nary_accounts_router)
app.include_router(yield_router)
app.include_router(csp_vault_router)
app.include_router(bridge_router)

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
