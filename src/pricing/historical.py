import logging
import time
from dataclasses import dataclass

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_client = httpx.AsyncClient(timeout=15.0)

_CACHE_TTL = 1800  # 30 minutes
_cache: list["PricePoint"] | None = None
_cache_ts: float = 0.0


@dataclass
class PricePoint:
    timestamp: float  # unix seconds
    price: float  # USD


async def get_eth_price_history(days: int = 7) -> list[PricePoint]:
    """Fetch daily ETH/USD prices for the last N days.

    Primary: CoinGecko (free, no API key).
    Fallback: Deribit OHLC.
    Returns one PricePoint per day, sorted chronologically.
    """
    global _cache, _cache_ts

    now = time.monotonic()
    if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    try:
        points = await _fetch_coingecko(days)
    except Exception:
        logger.warning("CoinGecko failed, trying Deribit fallback", exc_info=True)
        points = await _fetch_deribit(days)

    if not points:
        raise RuntimeError("No historical ETH price data available")

    _cache = points
    _cache_ts = time.monotonic()
    return points


async def _fetch_coingecko(days: int) -> list[PricePoint]:
    """CoinGecko market_chart — returns daily prices when days > 1."""
    resp = await _client.get(
        f"{settings.coingecko_api_url}/coins/ethereum/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
    )
    resp.raise_for_status()
    data = resp.json()

    prices = data.get("prices", [])
    if not prices:
        raise ValueError("CoinGecko returned empty prices")

    return [
        PricePoint(timestamp=ts / 1000, price=p)
        for ts, p in prices
    ]


async def _fetch_deribit(days: int) -> list[PricePoint]:
    """Deribit OHLC candles as fallback (1-day resolution)."""
    end_ts = int(time.time()) * 1000
    start_ts = end_ts - (days * 86_400_000)

    resp = await _client.get(
        "https://www.deribit.com/api/v2/public/get_tradingview_chart_data",
        params={
            "instrument_name": "ETH-PERPETUAL",
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "resolution": "1D",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", {})
    ticks = result.get("ticks", [])
    closes = result.get("close", [])

    if not ticks or not closes or len(ticks) != len(closes):
        raise ValueError("Deribit returned incomplete OHLC data")

    return [
        PricePoint(timestamp=ts / 1000, price=c)
        for ts, c in zip(ticks, closes)
    ]
