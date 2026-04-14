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
from src.api.yield_routes import router as yield_router
from src.bridge.routes import router as bridge_router
from src.config import settings, has_solana_config, has_xlayer_config, has_bridge_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


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

    tasks = []

    has_on_chain_config = (
        settings.batch_settler_address
        and settings.operator_private_key
        and settings.otoken_factory_address
    )
    if has_on_chain_config:
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
    if has_solana_config():
        from src.bots import (
            solana_circuit_breaker_bot,
            solana_event_indexer,
            solana_expiry_settler,
            solana_otoken_manager,
        )

        tasks.append(asyncio.create_task(solana_circuit_breaker_bot.run()))
        tasks.append(asyncio.create_task(solana_event_indexer.run()))
        tasks.append(asyncio.create_task(solana_expiry_settler.run()))
        tasks.append(asyncio.create_task(solana_otoken_manager.run()))
        logger.info(
            "Solana bots started (cluster=%s): circuit breaker, event indexer, expiry settler, otoken manager",
            settings.solana_cluster,
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
app.include_router(results_router)
app.include_router(analytics_router)
app.include_router(mm_router)
app.include_router(mm_ws_router)
app.include_router(activity_router)
app.include_router(leaderboard_router)
app.include_router(notifications_router)
app.include_router(yield_router)
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

    if has_xlayer_config() and settings.wokb_address:
        from src.api.xlayer_faucet import router as xlayer_faucet_router

        app.include_router(xlayer_faucet_router)
        logger.info("XLayer faucet enabled at /faucet/xlayer")

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
