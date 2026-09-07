import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from web3 import Web3

from src.contracts.web3_client import get_w3
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
        "inputs": [{"name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
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
        "inputs": [{"name": "_phaseId", "type": "uint16"}],
        "name": "phaseAggregators",
        "outputs": [{"name": "", "type": "address"}],
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
    {
        "inputs": [],
        "name": "description",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True)
class ValidatedChainlinkRound:
    answer_8dec: int
    updated_at: int
    source_decimals: int
    raw_answer: int
    round_id: int = 0


def normalize_to_8_decimals(answer: int, decimals: int) -> int:
    if not 0 <= decimals <= 36:
        raise ValueError(f"Chainlink decimals out of range: {decimals}")
    if decimals == 8:
        return answer
    if decimals > 8:
        return answer // (10 ** (decimals - 8))
    return answer * (10 ** (8 - decimals))


def read_validated_chainlink_round(
    w3: Web3,
    feed_address: str,
    *,
    expected_chain_id: int,
    expected_decimals: int,
    expected_description: str,
    max_age_seconds: int,
    not_before: int = 0,
    not_after: int | None = None,
    now: int | None = None,
    round_id: int | None = None,
    sequencer_feed_address: str = "",
    sequencer_grace_seconds: int = 3600,
) -> ValidatedChainlinkRound:
    """Read one complete, fresh round from an explicit provider/feed boundary."""
    if w3.eth.chain_id != expected_chain_id:
        raise ValueError(
            f"Oracle RPC chain mismatch: expected {expected_chain_id}, got {w3.eth.chain_id}"
        )
    now = now or int(datetime.now(timezone.utc).timestamp())
    if max_age_seconds < 0 or sequencer_grace_seconds < 0:
        raise ValueError("Oracle age and sequencer grace must be non-negative")
    if sequencer_feed_address:
        sequencer = w3.eth.contract(
            address=Web3.to_checksum_address(sequencer_feed_address),
            abi=AGGREGATOR_V3_ABI,
        )
        seq_round, seq_answer, seq_started, seq_updated, seq_answered = (
            sequencer.functions.latestRoundData().call()
        )
        if (
            seq_round <= 0
            or seq_answered < seq_round
            or seq_answer != 0
            or seq_started <= 0
            or seq_updated <= 0
            or seq_started > seq_updated
            or seq_updated > now
            or now - seq_started <= sequencer_grace_seconds
        ):
            raise ValueError(
                "L2 sequencer is down, incomplete, future, or in grace period"
            )

    feed = w3.eth.contract(
        address=Web3.to_checksum_address(feed_address), abi=AGGREGATOR_V3_ABI
    )
    decimals = int(feed.functions.decimals().call())
    description = str(feed.functions.description().call())
    if decimals != expected_decimals:
        raise ValueError(
            f"Chainlink feed decimals mismatch: expected {expected_decimals}, got {decimals}"
        )
    if description != expected_description:
        raise ValueError(
            f"Chainlink feed identity mismatch: expected {expected_description!r}, got {description!r}"
        )
    if round_id is not None and round_id <= 0:
        raise ValueError("Chainlink round ID must be positive")
    requested_round_id = round_id
    round_reader = (
        feed.functions.latestRoundData()
        if round_id is None
        else feed.functions.getRoundData(round_id)
    )
    round_id, answer, started_at, updated_at, answered_in_round = round_reader.call()
    if requested_round_id is not None and round_id != requested_round_id:
        raise ValueError("Chainlink returned a different round ID than requested")
    if round_id <= 0 or answered_in_round < round_id:
        raise ValueError("Chainlink round is incomplete")
    if answer <= 0:
        raise ValueError(f"Chainlink returned non-positive answer: {answer}")
    if started_at <= 0 or updated_at <= 0 or started_at > updated_at:
        raise ValueError("Chainlink round timestamps are incomplete")
    if updated_at > now or started_at > now:
        raise ValueError("Chainlink round timestamp is in the future")
    if now - updated_at > max_age_seconds:
        raise ValueError(
            f"Chainlink round is stale: age={now - updated_at}s max={max_age_seconds}s"
        )
    if updated_at < not_before:
        raise ValueError(
            f"Chainlink round predates expiry: updatedAt={updated_at} expiry={not_before}"
        )
    if not_after is not None and updated_at > not_after:
        raise ValueError(
            f"Chainlink round is after expiry window: updatedAt={updated_at} max={not_after}"
        )
    normalized = normalize_to_8_decimals(int(answer), decimals)
    if normalized <= 0:
        raise ValueError("Chainlink answer normalized to zero")
    return ValidatedChainlinkRound(
        normalized, int(updated_at), decimals, int(answer), int(round_id)
    )


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


def get_asset_price_raw(
    asset: Asset, *, now: int | None = None
) -> tuple[int, int, int]:
    """Read raw price from Chainlink for any supported asset.

    Returns (raw_answer, decimals, updated_at_timestamp). NVDAc is available
    only during the US regular session and must be at most one hour old.
    """
    if asset == Asset.NVDAC:
        from src.settlement_routing import read_new_asset_price_snapshot

        snapshot = read_new_asset_price_snapshot(asset.value, now=now)
        return snapshot.price_8, 8, snapshot.updated_at

    if asset in (Asset.CBZEC, Asset.CBHYPE, Asset.VVV):
        from src.settlement_routing import read_new_asset_price_snapshot

        snapshot = read_new_asset_price_snapshot(asset.value, now=now)
        return snapshot.price_8, 8, snapshot.updated_at

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
