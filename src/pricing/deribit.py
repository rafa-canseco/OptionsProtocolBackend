import httpx

from src.pricing.assets import Asset, get_asset_config

DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"

_client = httpx.AsyncClient(timeout=10.0)


async def get_iv(asset: Asset) -> float:
    """Fetch implied volatility from Deribit for any supported asset.

    Uses the book summary to get mark_iv from the nearest ATM option.
    For assets without Deribit listings, delegates to the IV proxy.
    Returns annualized IV as a decimal (e.g. 0.80 for 80%).
    """
    cfg = get_asset_config(asset)
    if not cfg.has_deribit:
        from src.pricing.iv_proxy import get_proxy_iv

        return await get_proxy_iv(asset)

    index_resp = await _client.get(
        f"{DERIBIT_BASE_URL}/public/get_index_price",
        params={"index_name": cfg.deribit_index},
    )
    index_resp.raise_for_status()
    index_data = index_resp.json()
    spot_price = index_data["result"]["index_price"]

    book_resp = await _client.get(
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

    return best / 100.0


async def get_index_price(asset: Asset) -> float:
    """Get USD index price from Deribit for any supported asset."""
    cfg = get_asset_config(asset)
    resp = await _client.get(
        f"{DERIBIT_BASE_URL}/public/get_index_price",
        params={"index_name": cfg.deribit_index},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["index_price"]


async def get_eth_iv() -> float:
    """Fetch ETH IV from Deribit (backward compat)."""
    return await get_iv(Asset.ETH)


async def get_eth_index_price() -> float:
    """Get ETH/USD index price from Deribit (backward compat)."""
    return await get_index_price(Asset.ETH)
