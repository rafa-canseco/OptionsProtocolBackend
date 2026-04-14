"""CoinGecko → MockChainlinkFeed price updater for XLayer testnet.

Periodically fetches the OKB/USD spot price from CoinGecko and writes
it to the MockChainlinkFeed contract on XLayer. The mock feed stores
prices with 8 decimals (e.g. $84.50 → 8_450_000_000).
"""

import asyncio
import logging

import httpx
from web3 import Web3

from src.config import settings
from src.contracts.web3_client import (
    get_operator_account,
    get_xlayer_w3,
    build_and_send_xlayer_tx,
)

logger = logging.getLogger(__name__)

MOCK_CHAINLINK_ABI = [
    {
        "inputs": [{"name": "_price", "type": "int256"}],
        "name": "setPrice",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

CHAINLINK_DECIMALS = 8
COINGECKO_URL = f"{settings.coingecko_api_url}/simple/price"


def _get_mock_feed():
    w3 = get_xlayer_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.chainlink_okb_usd_address),
        abi=MOCK_CHAINLINK_ABI,
    )


async def _fetch_okb_price() -> float:
    """Fetch OKB/USD spot from CoinGecko."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            COINGECKO_URL,
            params={"ids": "okb", "vs_currencies": "usd"},
        )
        resp.raise_for_status()
        return resp.json()["okb"]["usd"]


async def _update_price_once() -> None:
    price_usd = await _fetch_okb_price()
    raw_price = int(price_usd * 10**CHAINLINK_DECIMALS)

    feed = _get_mock_feed()
    account = get_operator_account()
    tx_fn = feed.functions.setPrice(raw_price)

    tx_hash = build_and_send_xlayer_tx(tx_fn, account)
    logger.info(
        "Updated MockChainlinkFeed: OKB=$%.2f (raw=%d) tx=%s",
        price_usd,
        raw_price,
        tx_hash,
    )


async def run() -> None:
    interval = settings.coingecko_price_update_interval_seconds
    logger.info("Starting CoinGecko price updater (interval=%ds)", interval)
    while True:
        try:
            await _update_price_once()
        except Exception:
            logger.exception("Price update failed, retrying next cycle")
        await asyncio.sleep(interval)
