import httpx

DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"

_client = httpx.AsyncClient(timeout=10.0)


async def get_eth_iv() -> float:
    """
    Fetch ETH implied volatility from Deribit.
    Uses the book summary to get mark_iv from the nearest ATM option.
    Returns annualized IV as a decimal (e.g. 0.80 for 80%).
    """
    # Get current ETH index price
    index_resp = await _client.get(
        f"{DERIBIT_BASE_URL}/public/get_index_price",
        params={"index_name": "eth_usd"},
    )
    index_data = index_resp.json()
    eth_price = index_data["result"]["index_price"]

    # Get all ETH option summaries
    book_resp = await _client.get(
        f"{DERIBIT_BASE_URL}/public/get_book_summary_by_currency",
        params={"currency": "ETH", "kind": "option"},
    )
    book_data = book_resp.json()
    options = book_data["result"]

    # Find the nearest ATM call with valid mark_iv
    best = None
    best_distance = float("inf")

    for opt in options:
        iv = opt.get("mark_iv")
        if not iv or iv <= 0:
            continue
        # instrument_name format: ETH-30DEC25-2800-C
        parts = opt["instrument_name"].split("-")
        if parts[-1] != "C":
            continue
        try:
            strike = float(parts[-2])
        except ValueError:
            continue
        distance = abs(strike - eth_price)
        if distance < best_distance:
            best_distance = distance
            best = iv

    if best is None:
        raise RuntimeError("No valid IV found from Deribit")

    # Deribit returns IV as percentage (e.g. 80 for 80%)
    return best / 100.0


async def get_eth_index_price() -> float:
    """Get ETH/USD index price from Deribit."""
    resp = await _client.get(
        f"{DERIBIT_BASE_URL}/public/get_index_price",
        params={"index_name": "eth_usd"},
    )
    data = resp.json()
    return data["result"]["index_price"]
