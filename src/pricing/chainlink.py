from web3 import Web3

from src.contracts.web3_client import get_w3
from src.pricing.assets import Asset, get_asset_config

AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_decimals_cache: dict[str, int] = {}
_feed_cache: dict[str, object] = {}


def _get_feed(asset: Asset):
    feed_address = get_asset_config(asset).chainlink_feed_address
    if feed_address not in _feed_cache:
        w3 = get_w3()
        _feed_cache[feed_address] = w3.eth.contract(
            address=Web3.to_checksum_address(feed_address),
            abi=AGGREGATOR_V3_ABI,
        )
    return _feed_cache[feed_address]


def _get_decimals(asset: Asset) -> int:
    feed_address = get_asset_config(asset).chainlink_feed_address
    if feed_address not in _decimals_cache:
        try:
            _decimals_cache[feed_address] = _get_feed(asset).functions.decimals().call()
        except Exception:
            _decimals_cache[feed_address] = 8
    return _decimals_cache[feed_address]


def get_asset_price_raw(asset: Asset) -> tuple[int, int, int]:
    """Read raw price from Chainlink for any supported asset.

    Returns (raw_answer, decimals, updated_at_timestamp).
    """
    feed = _get_feed(asset)
    decimals = _get_decimals(asset)
    (_, answer, _, updated_at, _) = feed.functions.latestRoundData().call()
    if answer <= 0:
        raise ValueError(
            f"Chainlink returned non-positive price for {asset.value}: {answer}"
        )
    return answer, decimals, updated_at


def get_asset_price(asset: Asset) -> tuple[float, int]:
    """Read USD price from Chainlink for any supported asset.

    Returns (price_float, updated_at_timestamp).
    """
    answer, decimals, updated_at = get_asset_price_raw(asset)
    return answer / (10**decimals), updated_at


def get_eth_price_raw() -> tuple[int, int, int]:
    """Read raw ETH/USD price from Chainlink (backward compat)."""
    return get_asset_price_raw(Asset.ETH)


def get_eth_price() -> tuple[float, int]:
    """Read ETH/USD price from Chainlink (backward compat)."""
    return get_asset_price(Asset.ETH)
