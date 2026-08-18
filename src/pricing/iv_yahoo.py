"""Live IV fetch from Yahoo Finance for equity-backed xStocks.

Yahoo's option chain endpoint returns per-contract IV already inverted
from bid/ask via Black-Scholes. We pick the ATM strike on the nearest
expiry >= today + 7 days and return that IV.

Yahoo TOS prohibits commercial redistribution of its raw data. We only
consume IV internally to price our own options; we do not redistribute
Yahoo data to end users. Migrate to a licensed vendor (Polygon, Alpaca,
ORATS) when product leaves read-only.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import yfinance as yf

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 600  # 10 minutes
_MIN_EXPIRY_DAYS = 7
_MAX_EXPIRY_DAYS = 45


@dataclass
class _CacheEntry:
    iv: float
    fetched_at: float


_cache: dict[str, _CacheEntry] = {}


def _fetch_tsla_iv_sync() -> float:
    """Synchronous Yahoo fetch — run in executor by the async wrapper.

    Strategy: pick the first listed expiry between 7 and 45 days out,
    take ATM IV from the call closest to spot. Yahoo's `impliedVolatility`
    column is already annualized decimal.
    """
    ticker = yf.Ticker("TSLA")
    expiries = ticker.options
    if not expiries:
        raise RuntimeError("Yahoo returned no option expiries for TSLA")

    now = time.time()
    today_epoch = now
    target_min = today_epoch + _MIN_EXPIRY_DAYS * 86400
    target_max = today_epoch + _MAX_EXPIRY_DAYS * 86400

    chosen_expiry: str | None = None
    for exp_str in expiries:
        # Yahoo returns YYYY-MM-DD strings; parse to epoch for comparison.
        exp_tuple = time.strptime(exp_str, "%Y-%m-%d")
        exp_epoch = time.mktime(exp_tuple)
        if target_min <= exp_epoch <= target_max:
            chosen_expiry = exp_str
            break

    if chosen_expiry is None:
        # Fall back to the nearest expiry >= today + 7 days even if beyond 45.
        for exp_str in expiries:
            exp_epoch = time.mktime(time.strptime(exp_str, "%Y-%m-%d"))
            if exp_epoch >= target_min:
                chosen_expiry = exp_str
                break

    if chosen_expiry is None:
        raise RuntimeError(
            f"No TSLA expiry >= {_MIN_EXPIRY_DAYS} days found in {expiries}"
        )

    chain = ticker.option_chain(chosen_expiry)
    calls = chain.calls
    if calls.empty:
        raise RuntimeError(f"Empty TSLA call chain for {chosen_expiry}")

    spot = ticker.fast_info.get("last_price")
    if not spot:
        # Fall back to mid of first call's bid/ask-based strike region.
        spot = float(calls.iloc[len(calls) // 2]["strike"])

    # Closest strike to spot.
    calls_with_iv = calls[calls["impliedVolatility"] > 0]
    if calls_with_iv.empty:
        raise RuntimeError(f"TSLA {chosen_expiry}: no calls with positive IV")
    idx = (calls_with_iv["strike"] - spot).abs().idxmin()
    atm_iv = float(calls_with_iv.loc[idx, "impliedVolatility"])

    if not (0.05 <= atm_iv <= 3.0):
        raise RuntimeError(f"TSLA IV {atm_iv:.4f} outside sane bounds [0.05, 3.0]")

    logger.info(
        "Yahoo TSLA IV: %.4f (expiry=%s, spot=%.2f, ATM strike=%s)",
        atm_iv,
        chosen_expiry,
        spot,
        calls_with_iv.loc[idx, "strike"],
    )
    return atm_iv


async def fetch_tsla_iv() -> float:
    """Return live TSLA ATM IV with in-memory cache.

    Raises on any fetch error so the caller can apply its own fallback.
    """
    cached = _cache.get("tsla")
    now = time.time()
    if cached and (now - cached.fetched_at) < _CACHE_TTL_SECONDS:
        return cached.iv

    loop = asyncio.get_running_loop()
    iv = await loop.run_in_executor(None, _fetch_tsla_iv_sync)
    _cache["tsla"] = _CacheEntry(iv=iv, fetched_at=now)
    return iv


def clear_cache() -> None:
    """Test helper."""
    _cache.clear()
