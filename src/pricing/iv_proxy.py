"""Synthetic IV proxy for assets without Deribit options.

Derives implied volatility by scaling ETH IV from Deribit using the
ratio of 30-day realized volatilities:

    IV_asset = IV_ETH * (RV_asset / RV_ETH)

Falls back to a hardcoded IV (0.80) if the proxy calculation fails.
"""

import logging
import math

import httpx

from src.config import settings
from src.pricing.assets import Asset

logger = logging.getLogger(__name__)

FALLBACK_IV = 0.80  # 80% annualized

# CoinGecko IDs for supported proxy assets
_COINGECKO_IDS: dict[str, str] = {
    "OKB": "okb",
    "ETH": "ethereum",
}

_client = httpx.AsyncClient(timeout=15.0)


async def _fetch_30d_prices(coingecko_id: str) -> list[float]:
    """Fetch 30-day daily closing prices from CoinGecko."""
    url = f"{settings.coingecko_api_url}/coins/{coingecko_id}/market_chart"
    resp = await _client.get(url, params={"vs_currency": "usd", "days": "30"})
    resp.raise_for_status()
    prices = resp.json()["prices"]
    return [p[1] for p in prices]


def _realized_vol(prices: list[float]) -> float:
    """Annualized realized volatility from a price series."""
    if len(prices) < 2:
        raise ValueError("Need at least 2 prices for realized vol")
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_vol = math.sqrt(variance)
    return daily_vol * math.sqrt(365)


async def get_proxy_iv(asset: Asset) -> float:
    """Derive IV for an asset without Deribit options.

    Returns annualized IV as a decimal (e.g. 0.80 for 80%).
    Falls back to FALLBACK_IV on any error.
    """
    from src.pricing.assets import get_asset_config

    cfg = get_asset_config(asset)

    cg_id = _COINGECKO_IDS.get(cfg.symbol)
    if cg_id is None:
        logger.warning(
            "No CoinGecko ID for %s, using fallback IV %.2f",
            cfg.symbol,
            FALLBACK_IV,
        )
        return FALLBACK_IV

    try:
        from src.pricing.deribit import get_iv as deribit_get_iv

        eth_iv = await deribit_get_iv(Asset.ETH)

        eth_prices = await _fetch_30d_prices("ethereum")
        asset_prices = await _fetch_30d_prices(cg_id)

        rv_eth = _realized_vol(eth_prices)
        rv_asset = _realized_vol(asset_prices)

        if rv_eth <= 0:
            logger.warning("ETH realized vol is zero, using fallback")
            return FALLBACK_IV

        proxy_iv = eth_iv * (rv_asset / rv_eth)
        logger.info(
            "Proxy IV for %s: %.4f (ETH_IV=%.4f, RV_%s=%.4f, RV_ETH=%.4f)",
            cfg.symbol,
            proxy_iv,
            eth_iv,
            cfg.symbol,
            rv_asset,
            rv_eth,
        )
        return proxy_iv

    except Exception:
        logger.warning(
            "IV proxy failed for %s, using fallback %.2f",
            cfg.symbol,
            FALLBACK_IV,
            exc_info=True,
        )
        return FALLBACK_IV
