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
from importlib import import_module

from src.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

BOTS = {
    "otoken_manager": "src.bots.otoken_manager",
    "event_indexer": "src.bots.event_indexer",
    "expiry_settler": "src.bots.expiry_settler",
    "circuit_breaker": "src.bots.circuit_breaker_bot",
    "yield_indexer": "src.bots.yield_indexer",
    "yield_airdrop": "src.bots.yield_airdrop",
}

LEGACY_BOTS = {
    "weekly_aggregator": "src.bots.weekly_aggregator",
}

# Preserve the pre-existing meaning of `all`: yield_airdrop remains explicit.
ALL_BOTS = (
    "otoken_manager",
    "event_indexer",
    "expiry_settler",
    "circuit_breaker",
    "yield_indexer",
)


def _available_bots() -> dict[str, str]:
    return {**BOTS, **LEGACY_BOTS}


def _enabled_all_bots() -> tuple[str, ...]:
    if settings.legacy_agora_v1_enabled:
        return (*ALL_BOTS, "weekly_aggregator")
    return ALL_BOTS


def _load_bot(bot_name: str):
    if bot_name in LEGACY_BOTS and not settings.legacy_agora_v1_enabled:
        raise RuntimeError(
            "weekly_aggregator is disabled; set LEGACY_AGORA_V1_ENABLED=true "
            "only for an explicit legacy rollback"
        )
    return import_module(_available_bots()[bot_name])


async def main(bot_name: str):
    if bot_name == "all":
        bots = [_load_bot(name) for name in _enabled_all_bots()]
        await asyncio.gather(*(bot.run() for bot in bots))
    elif bot_name in _available_bots():
        mod = _load_bot(bot_name)
        await mod.run()
    else:
        print(f"Unknown bot: {bot_name}")
        print(f"Available: {', '.join(_available_bots())}, all")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.bots.runner <bot_name>")
        print(f"Available: {', '.join(_available_bots())}, all")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
