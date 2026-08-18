"""Verified storage boundary for signed option-fund observations."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3


@dataclass(frozen=True, slots=True)
class OptionObservation:
    chain_id: int
    fund_address: str
    valuator_address: str
    adapter_address: str
    position_id: int
    snapshot_block: int
    snapshot_block_hash: str
    valid_until_block: int
    liability: int
    base_exit_cost: int
    observation_nonce: int
    signature: str

    def valuator_tuple(self) -> tuple[int, int, int, int, int, int, bytes]:
        return (
            self.position_id,
            self.snapshot_block,
            self.valid_until_block,
            self.liability,
            self.base_exit_cost,
            self.observation_nonce,
            Web3.to_bytes(hexstr=self.signature),
        )


class ObservationChain(Protocol):
    def chain_id(self) -> int: ...
    def block_hash(self, block_number: int) -> bytes: ...
    def head_block(self) -> int: ...
    def observation_digest(self, observation: OptionObservation) -> bytes: ...
    def observer_approved(self, valuator: str, observer: str, block: int) -> bool: ...
    def market_maker(self, adapter: str, position_id: int, block: int) -> str: ...
    def max_observation_window(self, valuator: str, block: int) -> int: ...


class ObservationStore(Protocol):
    def insert_verified(self, row: dict[str, Any]) -> None: ...


class ObservationIngestor:
    def __init__(self, chain: ObservationChain, store: ObservationStore):
        self.chain = chain
        self.store = store

    def ingest(self, observation: OptionObservation) -> str:
        self._validate_shape(observation)
        if observation.chain_id != self.chain.chain_id():
            raise ValueError("OBSERVATION_CHAIN_MISMATCH")
        expected_hash = Web3.to_hex(self.chain.block_hash(observation.snapshot_block))
        if expected_hash != observation.snapshot_block_hash:
            raise ValueError("OBSERVATION_BLOCK_HASH_MISMATCH")
        head = self.chain.head_block()
        max_window = self.chain.max_observation_window(
            observation.valuator_address, observation.snapshot_block
        )
        if observation.snapshot_block > head:
            raise ValueError("OBSERVATION_FROM_FUTURE")
        if not head <= observation.valid_until_block:
            raise ValueError("OBSERVATION_EXPIRED")
        if observation.valid_until_block > observation.snapshot_block + max_window:
            raise ValueError("OBSERVATION_WINDOW_TOO_LONG")
        digest = self.chain.observation_digest(observation)
        observer = Account._recover_hash(
            HexBytes(digest), signature=HexBytes(observation.signature)
        ).lower()
        if not self.chain.observer_approved(
            observation.valuator_address, observer, observation.snapshot_block
        ):
            raise ValueError("UNAPPROVED_OBSERVER")
        market_maker = self.chain.market_maker(
            observation.adapter_address,
            observation.position_id,
            observation.snapshot_block,
        ).lower()
        row = asdict(observation)
        row.update(
            fund_address=observation.fund_address.lower(),
            valuator_address=observation.valuator_address.lower(),
            adapter_address=observation.adapter_address.lower(),
            digest=Web3.to_hex(digest),
            observer_address=observer,
            market_maker_address=market_maker,
            verified_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.insert_verified(row)
        return observer

    @staticmethod
    def _validate_shape(observation: OptionObservation) -> None:
        addresses = (
            observation.fund_address,
            observation.valuator_address,
            observation.adapter_address,
        )
        if not all(Web3.is_address(value) for value in addresses):
            raise ValueError("INVALID_OBSERVATION_ADDRESS")
        values = (
            observation.position_id,
            observation.snapshot_block,
            observation.valid_until_block,
            observation.liability,
            observation.base_exit_cost,
            observation.observation_nonce,
        )
        if any(value < 0 for value in values):
            raise ValueError("INVALID_OBSERVATION_VALUE")
        if len(Web3.to_bytes(hexstr=observation.snapshot_block_hash)) != 32:
            raise ValueError("INVALID_OBSERVATION_BLOCK_HASH")
        if len(Web3.to_bytes(hexstr=observation.signature)) != 65:
            raise ValueError("INVALID_OBSERVATION_SIGNATURE")
