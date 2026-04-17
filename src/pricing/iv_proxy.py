"""Synthetic IV fallback for assets without Deribit options."""

import logging

from src.pricing.assets import Asset

logger = logging.getLogger(__name__)

FALLBACK_IV = 0.80  # 80% annualized


async def get_proxy_iv(asset: Asset) -> float:
    """Return fallback IV for assets without Deribit options."""
    logger.warning(
        "No Deribit options for %s, using fallback IV %.2f",
        asset.value,
        FALLBACK_IV,
    )
    return FALLBACK_IV
