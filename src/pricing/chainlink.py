from web3 import Web3

from src.config import settings
from src.contracts.web3_client import get_w3

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

_decimals_cache: int | None = None


def _get_feed():
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.chainlink_eth_usd_address),
        abi=AGGREGATOR_V3_ABI,
    )


def _get_decimals() -> int:
    global _decimals_cache
    if _decimals_cache is None:
        _decimals_cache = _get_feed().functions.decimals().call()
    return _decimals_cache


def get_eth_price_raw() -> tuple[int, int, int]:
    """Read raw ETH/USD price from Chainlink (no float conversion).

    Returns (raw_answer, decimals, updated_at_timestamp).
    Use this when integer precision matters (e.g., Oracle price setting).
    """
    feed = _get_feed()
    decimals = _get_decimals()
    (_, answer, _, updated_at, _) = feed.functions.latestRoundData().call()
    if answer <= 0:
        raise ValueError(f"Chainlink returned non-positive price: {answer}")
    return answer, decimals, updated_at


def get_eth_price() -> tuple[float, int]:
    """Read ETH/USD price from Chainlink on Base Sepolia.

    Returns (price_float, updated_at_timestamp).
    """
    answer, decimals, updated_at = get_eth_price_raw()
    return answer / (10**decimals), updated_at
