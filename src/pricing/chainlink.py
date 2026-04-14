import logging

from web3 import Web3

from src.chains import Chain
from src.contracts.web3_client import get_w3, get_xlayer_w3
from src.pricing.assets import Asset, get_asset_config

logger = logging.getLogger(__name__)

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
    cfg = get_asset_config(asset)
    feed_address = cfg.chainlink_feed_address
    if feed_address not in _feed_cache:
        w3 = get_xlayer_w3() if cfg.chain == Chain.XLAYER else get_w3()
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
            # Chainlink USD feeds use 8 decimals. Don't cache the fallback
            # so we retry on the next call in case RPC was transiently down.
            logger.warning(
                "Could not read decimals() for %s feed %s, using default 8",
                asset.value,
                feed_address,
                exc_info=True,
            )
            return 8
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


def get_asset_price_at_block(asset: Asset, block_number: int) -> float:
    """Read USD price from Chainlink at a specific historical block.

    Uses block_identifier override on latestRoundData() call.
    Requires an archive node or a provider that supports historical eth_call (e.g. Alchemy).
    Returns the price as a float.
    """
    feed = _get_feed(asset)
    decimals = _get_decimals(asset)
    (_, answer, _, _, _) = feed.functions.latestRoundData().call(
        block_identifier=block_number
    )
    if answer <= 0:
        raise ValueError(
            f"Chainlink returned non-positive price for {asset.value} at block"
            f" {block_number}: {answer}"
        )
    return answer / (10**decimals)


def get_eth_price_raw() -> tuple[int, int, int]:
    """Read raw ETH/USD price from Chainlink (backward compat)."""
    return get_asset_price_raw(Asset.ETH)


def get_eth_price() -> tuple[float, int]:
    """Read ETH/USD price from Chainlink (backward compat)."""
    return get_asset_price(Asset.ETH)
