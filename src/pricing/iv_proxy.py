"""Live / fallback IV for assets without Deribit options."""

import logging

from src.pricing.assets import Asset, get_asset_config
from src.pricing.iv_yahoo import fetch_tsla_iv

logger = logging.getLogger(__name__)


async def get_proxy_iv(asset: Asset) -> float:
    """Return IV for non-Deribit assets.

    For TSLAx: try Yahoo live IV first, fall back to the static
    `proxy_iv` from AssetConfig if Yahoo is unreachable or returns
    unusable data. For other proxy-only assets: return the static
    config value.

    Raises if the asset has no `proxy_iv` — the registry's import-time
    invariant should prevent that, so reaching this branch means the
    registry is inconsistent.
    """
    cfg = get_asset_config(asset)
    if cfg.proxy_iv is None:
        raise RuntimeError(
            f"No proxy_iv for {asset.value}. Add one to ASSET_CONFIGS in assets.py."
        )

    if asset == Asset.TSLAX:
        try:
            iv = await fetch_tsla_iv()
            logger.info("TSLAX live IV from Yahoo: %.4f", iv)
            return iv
        except Exception as exc:
            logger.warning(
                "Yahoo TSLA IV fetch failed (%s); using proxy IV %.2f",
                exc,
                cfg.proxy_iv,
            )
            return cfg.proxy_iv

    logger.warning(
        "No Deribit options for %s, using proxy IV %.2f",
        asset.value,
        cfg.proxy_iv,
    )
    return cfg.proxy_iv
