"""Disabled-by-default NAV reporter loop."""

import asyncio
import logging

from src.config import settings

logger = logging.getLogger(__name__)


async def run() -> None:
    """Run the configured reporter factory at the configured interval."""
    from src.fund_nav.runtime import (
        ReporterFleet,
        build_reporter,
        failure_backoff_seconds,
        report_run_failed,
    )

    def log_result(result) -> None:
        logger.info(
            "Fund NAV reporter run status=%s reason=%s",
            result.status,
            result.reason_code,
        )

    failure_count = 0
    while True:
        try:
            reporter = build_reporter()
            if isinstance(reporter, ReporterFleet):
                await reporter.run_forever(
                    settings.fund_nav_reporter_interval_seconds,
                    log_result,
                )
                return
            result = await asyncio.to_thread(reporter.run_once)
            log_result(result)
            failed = report_run_failed(result)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Fund NAV reporter run failed")
            failed = True
        if failed:
            failure_count += 1
            delay = failure_backoff_seconds(
                settings.fund_nav_reporter_interval_seconds,
                failure_count,
            )
        else:
            failure_count = 0
            delay = settings.fund_nav_reporter_interval_seconds
        await asyncio.sleep(delay)
