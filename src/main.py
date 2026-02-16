import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
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
        tasks.append(asyncio.create_task(expiry_settler.run()))
        tasks.append(asyncio.create_task(circuit_breaker_bot.run()))
        logger.info(f"Started {len(tasks)} background bots")
    else:
        logger.info("Bots not started: contract addresses or operator key not configured")

    yield

    for task in tasks:
        task.cancel()


app = FastAPI(title="Options Protocol", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://frontend-git-main-rcsc1s-projects.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}
