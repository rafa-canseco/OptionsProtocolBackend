from typing import Literal, NamedTuple

import httpx

from src.pricing.assets import Asset, get_asset_config
from src.pricing.iv_proxy import get_proxy_iv

DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"

_client = httpx.AsyncClient(timeout=10.0)

IVSource = Literal["deribit", "proxy"]


class IVResult(NamedTuple):
    """Annualized IV (decimal) plus the source it came from.

    Callers surfacing IV to users or MMs should check `source`: a
    `"proxy"` value means there is no live options market for the
    asset and the IV is a static fallback from AssetConfig.
    """

    value: float
    source: IVSource


async def get_iv(asset: Asset) -> IVResult:
    """Fetch implied volatility with the process-wide API client."""
    return await get_iv_with_client(asset, _client)


async def get_iv_with_client(asset: Asset, client: httpx.AsyncClient) -> IVResult:
    """Fetch implied volatility with a caller-owned bounded client."""
    cfg = get_asset_config(asset)
    if not cfg.has_deribit:
        return IVResult(await get_proxy_iv(asset), "proxy")

    index_resp = await client.get(
        f"{DERIBIT_BASE_URL}/public/get_index_price",
        params={"index_name": cfg.deribit_index},
    )
    index_resp.raise_for_status()
    index_data = index_resp.json()
    spot_price = index_data["result"]["index_price"]

    book_resp = await client.get(
        f"{DERIBIT_BASE_URL}/public/get_book_summary_by_currency",
        params={"currency": cfg.deribit_currency, "kind": "option"},
    )
    book_resp.raise_for_status()
    book_data = book_resp.json()
    options = book_data["result"]

    # When currency is shared (e.g. USDC hosts ETH_USDC, SOL_USDC, BTC_USDC),
    # filter instruments by the asset's index prefix (e.g. "SOL_USDC-").
    # For dedicated currencies (ETH, BTC), all instruments already match.
    instrument_prefix = (
        f"{cfg.deribit_index.upper()}-" if cfg.deribit_currency == "USDC" else ""
    )

    best = None
    best_distance = float("inf")

    for opt in options:
        iv = opt.get("mark_iv")
        if not iv or iv <= 0:
            continue
        name = opt["instrument_name"]
        if instrument_prefix and not name.startswith(instrument_prefix):
            continue
        parts = name.split("-")
        if parts[-1] != "C":
            continue
        try:
            strike = float(parts[-2])
        except ValueError:
            continue
        distance = abs(strike - spot_price)
        if distance < best_distance:
            best_distance = distance
            best = iv

    if best is None:
        raise RuntimeError(f"No valid IV found from Deribit for {cfg.deribit_currency}")

    return IVResult(best / 100.0, "deribit")


async def get_index_price(asset: Asset) -> float:
    """Get USD index price from Deribit for any supported asset."""
    cfg = get_asset_config(asset)
    if not cfg.has_deribit:
        raise ValueError(
            f"{asset.value} has no Deribit index. "
            "Use the chain-native oracle (Chainlink/Pyth) for spot."
        )
    resp = await _client.get(
        f"{DERIBIT_BASE_URL}/public/get_index_price",
        params={"index_name": cfg.deribit_index},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["index_price"]


async def get_eth_iv() -> float:
    """Fetch ETH IV from Deribit (backward compat)."""
    result = await get_iv(Asset.ETH)
    return result.value


async def get_eth_index_price() -> float:
    """Get ETH/USD index price from Deribit (backward compat)."""
    return await get_index_price(Asset.ETH)
