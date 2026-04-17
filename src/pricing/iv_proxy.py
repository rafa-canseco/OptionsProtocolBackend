"""Synthetic IV fallback for assets without Deribit options."""

import logging

from src.pricing.assets import Asset, get_asset_config

logger = logging.getLogger(__name__)


async def get_proxy_iv(asset: Asset) -> float:
    """Return the per-asset proxy IV configured in AssetConfig.

    Raises if the asset has no proxy_iv — the registry's import-time
    invariant should prevent that, so reaching this branch means the
    registry is inconsistent.
    """
    cfg = get_asset_config(asset)
    if cfg.proxy_iv is None:
        raise RuntimeError(
            f"No proxy_iv for {asset.value}. Add one to ASSET_CONFIGS in assets.py."
        )
    logger.warning(
        "No Deribit options for %s, using proxy IV %.2f",
        asset.value,
        cfg.proxy_iv,
    )
    return cfg.proxy_iv
