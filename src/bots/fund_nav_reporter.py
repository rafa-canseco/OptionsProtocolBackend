"""Disabled-by-default NAV reporter loop."""

import asyncio
import logging

from src.config import settings

logger = logging.getLogger(__name__)


async def run() -> None:
    """Run the configured reporter factory at the configured interval."""
    from src.fund_nav.runtime import build_reporter

    reporter = build_reporter()
    while True:
        try:
            result = await asyncio.to_thread(reporter.run_once)
            logger.info(
                "Fund NAV reporter run status=%s reason=%s",
                result.status,
                result.reason_code,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Fund NAV reporter run failed")
        await asyncio.sleep(settings.fund_nav_reporter_interval_seconds)
