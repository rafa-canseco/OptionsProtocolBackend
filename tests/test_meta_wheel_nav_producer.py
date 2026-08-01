from types import SimpleNamespace

import pytest
from web3 import Web3

from src.fund_nav.abis import ADAPTER_ABI
from src.fund_nav.runtime import (
    TrustedFund,
    ValuationContext,
    Web3ReporterGateway,
)
from src.vaults.csp_service import FundService
from tests.test_wheel_nav_observation_api import (
    CALL_LANE,
    CHAIN_ID,
    COORDINATOR,
    CSP_LANE,
    FUND,
    SNAPSHOT_BLOCK,
    SNAPSHOT_HASH,
    ObservationRepository,
)


META_VALUATOR = "0xf000000000000000000000000000000000000030"
CSP_VALUATOR = "0xf000000000000000000000000000000000000031"
CALL_VALUATOR = "0xf000000000000000000000000000000000000032"
CHILD_ADAPTER = "0xf000000000000000000000000000000000000040"
USDC = "0xf000000000000000000000000000000000000050"
WETH = "0xf000000000000000000000000000000000000051"
SPOT_FEED = "0xf000000000000000000000000000000000000052"
LANE_HASH = "0x" + "77" * 32
CHILD_DATA_HASH = "0x" + "88" * 32


class _Call:
    def __init__(self, value):
        self.value = value

    def call(self, *, block_identifier):
        assert block_identifier == SNAPSHOT_BLOCK
        return self.value


class _Functions:
    def __init__(self, values):
        self.values = values

    def __getattr__(self, name):
        def invoke(*args):
            value = self.values[name]
            if callable(value):
                value = value(*args)
            return _Call(value)

        return invoke


class _Eth:
    def __init__(self, *, duplicate_lane: bool = False):
        self.duplicate_lane = duplicate_lane

    def get_block(self, block):
        assert block == SNAPSHOT_BLOCK
        return {"timestamp": 2_000_000_000}

    def contract(self, *, address, abi):
        address = address.lower()
        function_names = {item.get("name") for item in abi}
        if address == COORDINATOR:
            lane_count = 2 if self.duplicate_lane else 1
            return SimpleNamespace(
                functions=_Functions(
                    {
                        "registeredLaneCount": lane_count,
                        "registeredLaneAt": lambda _index: (CSP_LANE, 0, True),
                    }
                )
            )
        if address == META_VALUATOR:
            return SimpleNamespace(
                functions=_Functions(
                    {
                        "cspValuator": CSP_VALUATOR,
                        "coveredCallValuator": CALL_VALUATOR,
                        "spotFeed": SPOT_FEED,
                        "spotFeedDecimals": 8,
                        "maxSpotStaleness": 600,
                        "transitionExitCostBps": 25,
                    }
                )
            )
        if address == CSP_VALUATOR:
            return SimpleNamespace(
                functions=_Functions(
                    {
                        "valuePosition": (
                            1_000_000,
                            100_000,
                            500_000,
                            10_000,
                            Web3.to_bytes(hexstr=CHILD_DATA_HASH),
                        ),
                        "maxObservationWindow": 120,
                    }
                )
            )
        if address == SPOT_FEED:
            return SimpleNamespace(
                functions=_Functions(
                    {
                        "decimals": 8,
                        "latestRoundData": (
                            10,
                            2000 * 10**8,
                            0,
                            1_999_999_990,
                            10,
                        ),
                    }
                )
            )
        if address == CSP_LANE and "accountingState" in function_names:
            return SimpleNamespace(functions=_Functions({"accountingState": (100, 0)}))
        if address == CSP_LANE:
            return SimpleNamespace(
                functions=_Functions(
                    {
                        "childShares": 100,
                        "adapter": CHILD_ADAPTER,
                        "activePositionId": 1,
                        "positionStateHash": Web3.to_bytes(hexstr=LANE_HASH),
                    }
                )
            )
        raise AssertionError(f"unexpected contract {address}")


class _ProducerRepository:
    def __init__(self):
        self.valuations = {}
        self.snapshot = None

    def store_wheel_lane_valuation(self, row):
        self.valuations[row["child_vault"]] = row

    def wheel_lane_valuations(self, *_args):
        return list(self.valuations.values())

    def store_wheel_nav_snapshot(self, row):
        self.snapshot = row


def _gateway(*, duplicate_lane: bool = False):
    repository = _ProducerRepository()
    gateway = object.__new__(Web3ReporterGateway)
    gateway.w3 = SimpleNamespace(eth=_Eth(duplicate_lane=duplicate_lane))
    gateway.fund = TrustedFund(
        registry={
            "chain_id": CHAIN_ID,
            "fund_address": FUND,
            "accounting_asset": USDC,
            "weth": WETH,
            "strategy_kind": "meta_wheel",
        },
        state={},
        contracts={},
        trust_reason=None,
    )
    gateway.repository = repository
    gateway.addresses = {
        "wheel_coordinator": COORDINATOR,
        "meta_wheel_valuator": META_VALUATOR,
    }
    gateway.wheel_valuation_contexts = {
        "csp": ValuationContext("csp", ADAPTER_ABI, 11, (), None),
    }
    gateway._observation_adapter_abis = {}
    gateway.strategy_kind = "meta_wheel"
    gateway._pending_wheel_nav_snapshot = None
    gateway._valuation_observations = lambda *_args, **_kwargs: []
    gateway._require_lane_custody = lambda *_args, **_kwargs: None
    gateway._token_balance = lambda token, *_args: 600 if token == USDC else 0
    gateway.vault = SimpleNamespace(functions=_Functions({"accountedIdleAssets": 200}))
    gateway.accounting = SimpleNamespace(functions=_Functions({"lastReportNonce": 6}))
    return gateway, repository


def test_producer_persists_snapshot_consumed_by_authenticated_endpoint() -> None:
    gateway, producer = _gateway()
    reports = gateway._wheel_lane_valuations(
        SNAPSHOT_BLOCK, Web3.to_bytes(hexstr=SNAPSHOT_HASH)
    )
    meta_valuator = gateway.w3.eth.contract(address=META_VALUATOR, abi=[])
    gateway._prepare_wheel_nav_snapshot(
        block=SNAPSHOT_BLOCK,
        block_hash=Web3.to_bytes(hexstr=SNAPSHOT_HASH),
        reports=reports,
        coordinator_state=(1, 1, 0, 500, 100, 100, 0, 600, 0),
        parent_value=(1_000_700, 100_000, 600, 10_000, bytes(32)),
        valuator=meta_valuator,
    )
    gateway.persist_confirmed_wheel_nav_snapshot()

    assert (
        producer.snapshot["child_reports"][0]["expected_position_state_hash"]
        == LANE_HASH
    )
    endpoint_repository = ObservationRepository()
    endpoint_repository.rows["nav"] = producer.snapshot
    endpoint_repository.rows["valuations"] = list(producer.valuations.values())
    endpoint_repository.rows["lane_registry"] = [
        {
            "child_vault": CSP_LANE,
            "registration_index": 0,
            "lane_type": "csp",
        },
        {
            "child_vault": CALL_LANE,
            "registration_index": 1,
            "lane_type": "covered_call",
        },
    ]

    observation = FundService(endpoint_repository).wheel_nav_observation(
        "base-sepolia:meta-wheel", SNAPSHOT_BLOCK
    )

    assert [lane.lane for lane in observation.lanes] == [CSP_LANE]
    assert observation.lanes[0].position_state_hash == LANE_HASH


def test_producer_rejects_duplicate_custody_before_persistence() -> None:
    gateway, producer = _gateway(duplicate_lane=True)

    with pytest.raises(RuntimeError, match="DUPLICATE_WHEEL_CUSTODY_DOMAIN"):
        gateway._produce_wheel_lane_valuations(
            SNAPSHOT_BLOCK, Web3.to_bytes(hexstr=SNAPSHOT_HASH)
        )

    assert producer.valuations == {}


def test_producer_propagates_missing_child_observation_quorum() -> None:
    gateway, producer = _gateway()
    gateway._valuation_observations = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("INSUFFICIENT_OBSERVATION_QUORUM")
    )

    with pytest.raises(RuntimeError, match="INSUFFICIENT_OBSERVATION_QUORUM"):
        gateway._produce_wheel_lane_valuations(
            SNAPSHOT_BLOCK, Web3.to_bytes(hexstr=SNAPSHOT_HASH)
        )

    assert producer.valuations == {}
