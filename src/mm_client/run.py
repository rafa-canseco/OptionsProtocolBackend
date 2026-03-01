"""Main loop: get market -> price -> sign -> submit -> sleep."""
import asyncio
import logging

from src.mm_client.client import MMApiClient
from src.mm_client.config import MMClientSettings
from src.mm_client.quoter import (
    build_domain,
    generate_signed_quotes,
    get_maker_nonce,
    get_mm_address,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def publish_cycle(
    api: MMApiClient,
    cfg: MMClientSettings,
    domain: dict,
    mm_address: str,
) -> None:
    """Single publish cycle."""
    market = await api.get_market()
    logger.info(
        "Market: spot=%.2f iv=%.4f otokens=%d",
        market["eth_spot"],
        market["eth_iv"],
        len(market.get("available_otokens", [])),
    )

    maker_nonce = await asyncio.to_thread(
        get_maker_nonce,
        cfg.rpc_url,
        cfg.batch_settler_address,
        mm_address,
    )

    quotes = generate_signed_quotes(
        market=market,
        private_key=cfg.mm_private_key,
        domain=domain,
        spread=cfg.spread,
        deadline_seconds=cfg.quote_deadline_seconds,
        max_amount=cfg.max_amount_otokens,
        maker_nonce=maker_nonce,
    )

    if not quotes:
        logger.warning("No quotes generated, skipping submission")
        return

    result = await api.submit_quotes(quotes)
    logger.info(
        "Submitted %d quotes: accepted=%d rejected=%d",
        len(quotes),
        result.get("accepted", 0),
        result.get("rejected", 0),
    )
    if result.get("errors"):
        for err in result["errors"]:
            logger.warning("Rejection: %s", err)


async def main():
    """Entry point for the standalone MM client."""
    cfg = MMClientSettings()
    api = MMApiClient(cfg)
    domain = build_domain(cfg.chain_id, cfg.batch_settler_address)
    mm_address = get_mm_address(cfg.mm_private_key)

    logger.info("MM client starting")
    logger.info("  MM address: %s", mm_address)
    logger.info("  API: %s", cfg.api_base_url)
    logger.info("  Chain ID: %d", cfg.chain_id)
    logger.info("  Spread: %.2f%%", cfg.spread * 100)
    logger.info("  Interval: %ds", cfg.publish_interval_seconds)

    while True:
        try:
            await publish_cycle(api, cfg, domain, mm_address)
        except Exception:
            logger.exception("Publish cycle failed")
        await asyncio.sleep(cfg.publish_interval_seconds)
