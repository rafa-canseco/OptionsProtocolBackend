from dataclasses import replace

import pytest
from eth_account import Account
from web3 import Web3

from src.fund_nav.models import sign_digest
from src.fund_nav.observations import ObservationIngestor, OptionObservation

FUND = "0xf000000000000000000000000000000000000001"
VALUATOR = "0xf000000000000000000000000000000000000002"
ADAPTER = "0xf000000000000000000000000000000000000003"
MM_KEY = "0x" + f"{1:064x}"
OBSERVER_KEY = "0x" + f"{2:064x}"
MM = Account.from_key(MM_KEY).address.lower()
OBSERVER = Account.from_key(OBSERVER_KEY).address.lower()
BLOCK_HASH = "0x" + "12" * 32


class Chain:
    def __init__(self):
        self.approved = {MM, OBSERVER}

    def chain_id(self):
        return 84532

    def block_hash(self, _block):
        return Web3.to_bytes(hexstr=BLOCK_HASH)

    def head_block(self):
        return 101

    def observation_digest(self, observation):
        return Web3.keccak(
            text=f"{observation.position_id}:{observation.observation_nonce}"
        )

    def observer_approved(self, _valuator, observer, _block):
        return observer in self.approved

    def market_maker(self, _adapter, _position_id, _block):
        return MM

    def max_observation_window(self, _valuator, _block):
        return 20


class Store:
    def __init__(self):
        self.rows = []

    def insert_verified(self, row):
        identity = (row["position_id"], row["snapshot_block"], row["observer_address"])
        if any(
            (item["position_id"], item["snapshot_block"], item["observer_address"])
            == identity
            for item in self.rows
        ):
            raise ValueError("duplicate observation")
        self.rows.append(row)


def observation(key=OBSERVER_KEY, nonce=7):
    unsigned = OptionObservation(
        chain_id=84532,
        fund_address=FUND,
        valuator_address=VALUATOR,
        adapter_address=ADAPTER,
        position_id=1,
        snapshot_block=100,
        snapshot_block_hash=BLOCK_HASH,
        valid_until_block=110,
        liability=25,
        base_exit_cost=2,
        observation_nonce=nonce,
        signature="0x" + "00" * 65,
    )
    digest = Chain().observation_digest(unsigned)
    return replace(unsigned, signature=Web3.to_hex(sign_digest(digest, key)))


def test_ingestion_recovers_and_stores_only_approved_bound_observation() -> None:
    store = Store()
    observer = ObservationIngestor(Chain(), store).ingest(observation())

    assert observer == OBSERVER
    assert store.rows[0]["digest"] == Web3.to_hex(
        Chain().observation_digest(observation())
    )
    assert store.rows[0]["market_maker_address"] == MM


def test_duplicate_observer_snapshot_position_is_rejected() -> None:
    store = Store()
    ingestor = ObservationIngestor(Chain(), store)
    ingestor.ingest(observation())

    with pytest.raises(ValueError, match="duplicate observation"):
        ingestor.ingest(observation(nonce=8))


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda item: replace(item, snapshot_block_hash="0x" + "00" * 32),
            "BLOCK_HASH",
        ),
        (lambda item: replace(item, valid_until_block=99), "EXPIRED"),
        (lambda item: replace(item, valid_until_block=121), "WINDOW_TOO_LONG"),
        (lambda item: replace(item, signature="0x01"), "SIGNATURE"),
    ],
)
def test_malformed_or_stale_observation_is_rejected(mutation, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        ObservationIngestor(Chain(), Store()).ingest(mutation(observation()))


def test_unapproved_observer_is_rejected() -> None:
    chain = Chain()
    chain.approved.remove(OBSERVER)

    with pytest.raises(ValueError, match="UNAPPROVED_OBSERVER"):
        ObservationIngestor(chain, Store()).ingest(observation())
