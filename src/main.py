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


app = FastAPI(title="Options Protocol", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://frontend-seven-psi-40.vercel.app",
        "https://frontend-git-main-rcsc1s-projects.vercel.app",
    ],
    allow_origin_regex=r"https://frontend-git-.*-rcsc1s-projects\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(results_router)
app.include_router(analytics_router)

if settings.beta_mode:
    from src.api.demo import router as demo_router
    app.include_router(demo_router)
    logger.info("Beta mode: /demo/settle endpoint enabled")


@app.get("/health")
async def health():
    return {"status": "ok"}
