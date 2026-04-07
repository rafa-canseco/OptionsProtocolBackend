"""Pyth oracle reads for Solana assets.

Uses the Pyth HTTP API (Hermes) to fetch prices without
requiring an on-chain Pyth account read. This avoids the
complexity of deserializing PriceUpdateV2 accounts from
the backend -- the on-chain Pyth flow is used by the
Solana programs directly.
"""

import logging
import time

import httpx

from src.chains import Chain
from src.pricing.assets import Asset, get_asset_config

logger = logging.getLogger(__name__)

HERMES_URL = "https://hermes.pyth.network/v2/updates/price/latest"

# Cache: {asset_symbol: (price, updated_at, cached_at_monotonic)}
_cache: dict[str, tuple[float, int, float]] = {}
_CACHE_TTL = 5  # seconds


def get_pyth_price(asset: Asset) -> tuple[float, int]:
    """Fetch USD price from Pyth Hermes API.

    Returns (price_float, publish_time_unix).
    """
    cfg = get_asset_config(asset)
    if cfg.chain != Chain.SOLANA:
        raise ValueError(f"{asset.value} is not a Solana asset. Use Chainlink.")

    feed_id = cfg.pyth_feed_id
    if not feed_id:
        raise ValueError(f"No Pyth feed ID configured for {asset.value}")

    now = time.monotonic()
    cached = _cache.get(cfg.symbol)
    if cached and (now - cached[2]) < _CACHE_TTL:
        return cached[0], cached[1]

    try:
        resp = httpx.get(
            HERMES_URL,
            params={"ids[]": feed_id, "parsed": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Pyth Hermes API request failed for {asset.value}") from exc

    try:
        parsed = data["parsed"]
    except KeyError:
        raise ValueError(
            f"Unexpected Pyth response format for {asset.value}: missing 'parsed' key"
        )

    if not parsed:
        raise ValueError(
            f"Pyth returned no data for {asset.value} (feed {feed_id[:16]}...)"
        )

    try:
        price_msg = parsed[0]["price"]
        price_raw = int(price_msg["price"])
        exponent = int(price_msg["expo"])
        publish_time = int(price_msg["publish_time"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(
            f"Failed to parse Pyth price fields for {asset.value}: {exc}"
        ) from exc

    if price_raw <= 0:
        raise ValueError(
            f"Pyth returned non-positive price for {asset.value}: {price_raw}"
        )

    price_float = price_raw * (10**exponent)
    _cache[cfg.symbol] = (price_float, publish_time, now)

    return price_float, publish_time


def get_spot_price(asset: Asset) -> tuple[float, int]:
    """Alias matching the Base client interface signature."""
    return get_pyth_price(asset)
