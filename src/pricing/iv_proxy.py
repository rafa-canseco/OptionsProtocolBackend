"""Live / fallback IV for assets without Deribit options."""

import logging
import math
import time

import httpx

from src.pricing.assets import Asset, get_asset_config
from src.pricing.iv_yahoo import fetch_tsla_iv

logger = logging.getLogger(__name__)

_HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
_HYPERLIQUID_INTERVAL = "1h"
_HYPERLIQUID_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
_HYPERLIQUID_CACHE_TTL_SECONDS = 600
_HYPERLIQUID_CANDLE_MS = 60 * 60 * 1000
_MIN_COVERAGE_CANDLES = 24 * 20
_MIN_IV = 0.05
_MAX_IV = 3.0

_HYPERLIQUID_COINS: dict[Asset, str] = {
    Asset.NVDAC: "xyz:NVDA",
    Asset.CBZEC: "ZEC",
    Asset.CBHYPE: "HYPE",
    Asset.VVV: "VVV",
}

_hyperliquid_cache: dict[Asset, tuple[float, float]] = {}


async def fetch_hyperliquid_realized_iv(asset: Asset) -> float:
    """Estimate annualized IV from 30-day hourly Hyperliquid closes.

    Hyperliquid is the approved hedge/market-data source for the four Base
    assets without Deribit options. This is a realized-vol proxy, not a claim
    of listed implied volatility.
    """
    coin = _HYPERLIQUID_COINS.get(asset)
    if coin is None:
        raise ValueError(f"No Hyperliquid IV coin configured for {asset.value}")

    now = time.time()
    cached = _hyperliquid_cache.get(asset)
    if cached and now - cached[1] < _HYPERLIQUID_CACHE_TTL_SECONDS:
        return cached[0]

    end_ms = int(now * 1000)
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": _HYPERLIQUID_INTERVAL,
            "startTime": end_ms - _HYPERLIQUID_LOOKBACK_SECONDS * 1000,
            "endTime": end_ms,
        },
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(_HYPERLIQUID_INFO_URL, json=payload)
        response.raise_for_status()
        rows = response.json()

    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Hyperliquid returned no candles for {coin}")
    end_ms = int(now * 1000)
    completed = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("s") != coin
            or row.get("i") != _HYPERLIQUID_INTERVAL
        ):
            raise ValueError(f"Malformed Hyperliquid candle metadata for {coin}")
        start_ms = int(row.get("t", 0))
        close_ms = int(row.get("T", 0))
        if start_ms <= 0 or close_ms < start_ms or close_ms > end_ms:
            continue
        completed.append((start_ms, close_ms, float(row["c"])))
    completed.sort(key=lambda candle: candle[0])
    timestamps = [candle[0] for candle in completed]
    if len(completed) < _MIN_COVERAGE_CANDLES or len(set(timestamps)) != len(
        timestamps
    ):
        raise RuntimeError(
            f"Hyperliquid returned insufficient candle coverage for {coin}"
        )
    if any(
        current - previous > 2 * _HYPERLIQUID_CANDLE_MS
        for previous, current in zip(timestamps, timestamps[1:])
    ):
        raise RuntimeError(f"Hyperliquid candle series has gaps for {coin}")
    if end_ms - completed[-1][1] > 3 * _HYPERLIQUID_CANDLE_MS:
        raise RuntimeError(f"Hyperliquid candles are stale for {coin}")

    closes = [close for _, _, close in completed if math.isfinite(close) and close > 0]
    if len(closes) != len(completed):
        raise ValueError(f"Hyperliquid candle close is invalid for {coin}")
    returns = [
        math.log(current / previous)
        for previous, current in zip(closes, closes[1:])
        if previous > 0 and current > 0
    ]
    if len(returns) < 24:
        raise RuntimeError(f"Hyperliquid returned too few candles for {coin}")

    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    iv = math.sqrt(variance * 24 * 365)
    if not _MIN_IV <= iv <= _MAX_IV:
        raise ValueError(f"Hyperliquid realized IV {iv:.4f} outside safe bounds")

    _hyperliquid_cache[asset] = (iv, now)
    logger.info("Hyperliquid realized-vol proxy: %s=%.4f", coin, iv)
    return iv


async def get_proxy_iv(asset: Asset) -> float:
    """Return live proxy IV, falling back to the registered bounded value."""
    cfg = get_asset_config(asset)
    if cfg.proxy_iv is None:
        raise RuntimeError(
            f"No proxy_iv for {asset.value}. Add one to ASSET_CONFIGS in assets.py."
        )

    if asset in _HYPERLIQUID_COINS:
        try:
            return await fetch_hyperliquid_realized_iv(asset)
        except Exception as exc:
            logger.warning(
                "Hyperliquid IV fetch failed for %s (%s); using proxy IV %.2f",
                asset.value,
                exc,
                cfg.proxy_iv,
            )
            return cfg.proxy_iv

    if asset == Asset.TSLAX:
        try:
            iv = await fetch_tsla_iv()
            logger.info("Yahoo TSLA IV from Yahoo: %.4f", iv)
            return iv
        except Exception as exc:
            logger.warning(
                "Yahoo TSLA IV fetch failed (%s); using proxy IV %.2f",
                exc,
                cfg.proxy_iv,
            )
            return cfg.proxy_iv

    logger.warning(
        "No Deribit options for %s, using proxy IV %.2f", asset.value, cfg.proxy_iv
    )
    return cfg.proxy_iv


def clear_cache() -> None:
    """Test helper."""
    _hyperliquid_cache.clear()
