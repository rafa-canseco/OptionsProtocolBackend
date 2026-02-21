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


async def get_eth_price_history(
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> list[PricePoint]:
    """Fetch daily ETH/USD prices.

    When start_ts/end_ts are provided, fetches that specific range (no cache).
    Otherwise fetches the last 7 days with 30-minute caching.

    Primary: CoinGecko (free, no API key).
    Fallback: Deribit OHLC.
    Returns approximately one PricePoint per day, sorted chronologically.
    """
    global _cache, _cache_ts

    use_cache = start_ts is None and end_ts is None

    if use_cache:
        now = time.monotonic()
        if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache

    try:
        points = await _fetch_coingecko(start_ts, end_ts)
    except (httpx.HTTPError, ValueError) as cg_err:
        logger.error("CoinGecko failed (%s), trying Deribit fallback", cg_err, exc_info=True)
        try:
            points = await _fetch_deribit(start_ts, end_ts)
        except (httpx.HTTPError, ValueError) as db_err:
            logger.error(
                "Both CoinGecko and Deribit failed. CoinGecko: %s, Deribit: %s",
                cg_err, db_err,
            )
            if use_cache and _cache is not None:
                logger.warning("Serving stale cache (age: %.0fs)", now - _cache_ts)
                return _cache
            raise RuntimeError(
                f"All price sources failed. CoinGecko: {cg_err}, Deribit: {db_err}"
            ) from db_err

    if len(points) < 2:
        raise RuntimeError(f"Insufficient price data: got {len(points)} points, need at least 2")

    if use_cache:
        _cache = points
        _cache_ts = time.monotonic()
    return points


async def _fetch_coingecko(
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> list[PricePoint]:
    """CoinGecko market_chart — returns daily prices."""
    if start_ts is not None and end_ts is not None:
        resp = await _client.get(
            f"{settings.coingecko_api_url}/coins/ethereum/market_chart/range",
            params={"vs_currency": "usd", "from": int(start_ts), "to": int(end_ts)},
        )
    else:
        resp = await _client.get(
            f"{settings.coingecko_api_url}/coins/ethereum/market_chart",
            params={"vs_currency": "usd", "days": 7, "interval": "daily"},
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


async def _fetch_deribit(
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> list[PricePoint]:
    """Deribit OHLC candles as fallback (1-day resolution)."""
    if start_ts is not None and end_ts is not None:
        start_ms = int(start_ts) * 1000
        end_ms = int(end_ts) * 1000
    else:
        end_ms = int(time.time()) * 1000
        start_ms = end_ms - (7 * 86_400_000)

    resp = await _client.get(
        "https://www.deribit.com/api/v2/public/get_tradingview_chart_data",
        params={
            "instrument_name": "ETH-PERPETUAL",
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
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
