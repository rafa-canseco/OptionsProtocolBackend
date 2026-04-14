"""
Standalone bot runner.

Usage:
    uv run python -m src.bots.runner otoken_manager
    uv run python -m src.bots.runner event_indexer
    uv run python -m src.bots.runner expiry_settler
    uv run python -m src.bots.runner circuit_breaker
    uv run python -m src.bots.runner all
"""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

BOTS = {
    "otoken_manager": "src.bots.otoken_manager",
    "event_indexer": "src.bots.event_indexer",
    "expiry_settler": "src.bots.expiry_settler",
    "circuit_breaker": "src.bots.circuit_breaker_bot",
    "weekly_aggregator": "src.bots.weekly_aggregator",
    "yield_indexer": "src.bots.yield_indexer",
    "yield_airdrop": "src.bots.yield_airdrop",
    "price_updater": "src.bots.price_updater",
    # XLayer
    "xlayer_otoken_manager": "src.bots.xlayer_otoken_manager",
    "xlayer_event_indexer": "src.bots.xlayer_event_indexer",
    "xlayer_expiry_settler": "src.bots.xlayer_expiry_settler",
    "xlayer_circuit_breaker": "src.bots.xlayer_circuit_breaker_bot",
}


async def main(bot_name: str):
    if bot_name == "all":
        from src.bots import (
            otoken_manager,
            event_indexer,
            expiry_settler,
            circuit_breaker_bot,
            weekly_aggregator,
            yield_indexer,
        )

        await asyncio.gather(
            otoken_manager.run(),
            event_indexer.run(),
            expiry_settler.run(),
            circuit_breaker_bot.run(),
            weekly_aggregator.run(),
            yield_indexer.run(),
        )
    elif bot_name in BOTS:
        import importlib

        mod = importlib.import_module(BOTS[bot_name])
        await mod.run()
    else:
        print(f"Unknown bot: {bot_name}")
        print(f"Available: {', '.join(BOTS.keys())}, all")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.bots.runner <bot_name>")
        print(f"Available: {', '.join(BOTS.keys())}, all")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
