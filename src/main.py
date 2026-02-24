import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
from src.api.results import router as results_router
from src.api.analytics import router as analytics_router
from src.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background bots when contract addresses are configured."""
    tasks = []

    if settings.price_sheet_address and settings.operator_private_key:
        from src.bots import (
            price_publisher,
            event_indexer,
            expiry_settler,
            circuit_breaker_bot,
        )

        tasks.append(asyncio.create_task(price_publisher.run()))
        tasks.append(asyncio.create_task(event_indexer.run()))
        if not settings.beta_mode:
            tasks.append(asyncio.create_task(expiry_settler.run()))
        else:
            logger.info("Beta mode: expiry_settler disabled (settlement is user-triggered)")
        tasks.append(asyncio.create_task(circuit_breaker_bot.run()))
        logger.info("Started %d on-chain bots", len(tasks))
    else:
        logger.info("On-chain bots not started: contract addresses or operator key not configured")

    # Weekly aggregator only needs DB access, not on-chain config
    from src.bots import weekly_aggregator
    tasks.append(asyncio.create_task(weekly_aggregator.run()))
    logger.info("Weekly aggregator started")

    yield

    for task in tasks:
        task.cancel()


openapi_tags = [
    {
        "name": "Market Data",
        "description": "Live ETH option prices computed via Black-Scholes. Prices refresh every ~15 s and include on-chain oToken addresses when available.",
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
        "name": "Faucet",
        "description": "Send gas ETH + test tokens (LETH/LUSD) on Base Sepolia. 1 claim per wallet (permanent). Beta only — disabled in production.",
    },
    {
        "name": "Demo",
        "description": "Beta-only endpoints for triggering instant settlement in testnet. Requires X-Demo-Key header. Disabled in production.",
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
        "1. `GET /prices` — fetch the current option price menu\n"
        "2. Pick a quote and call `executeOrder()` on the BatchSettler contract from the user's wallet\n"
        "3. `GET /positions/{address}` — check the user's open and settled positions\n\n"
        "All monetary values are in USD unless noted. On-chain amounts use the token's native decimals "
        "(oToken = 8, USDC = 6, WETH = 18).\n\n"
        "**Chain:** Base Sepolia (chain ID 84532)  \n"
        "**Contracts:** see [BaseScan](https://sepolia.basescan.org)"
    ),
    version="0.3.0",
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(results_router)
app.include_router(analytics_router)

if settings.beta_mode:
    from src.api.demo import router as demo_router
    from src.api.faucet import router as faucet_router
    app.include_router(demo_router)
    app.include_router(faucet_router)
    logger.info("Beta mode: /demo/settle and /faucet endpoints enabled")


@app.get("/health", tags=["System"], summary="Health check")
async def health():
    """Returns `{\"status\": \"ok\"}` when the API is running."""
    return {"status": "ok"}
