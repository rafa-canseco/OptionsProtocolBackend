import asyncio
from types import SimpleNamespace

import pytest
from eth_account import Account
from web3 import Web3

from src.bots import fund_nav_reporter
from src.fund_nav import runtime
from src.fund_nav.models import sign_digest
from src.fund_nav.observations import OptionObservation
from src.fund_nav.reporter import ReportRun, SignedTransaction
from src.fund_nav.runtime import (
    BlockedReporter,
    RuntimeReporter,
    TrustedFund,
    TrustedRegistryLoader,
    Web3ReporterGateway,
    encode_valuation_data,
)
from src.vaults.csp_service import PROXY_ROLES, REQUIRED_TRUSTED_ROLES

FUND = "0xf000000000000000000000000000000000000001"
VALUATOR = "0xf000000000000000000000000000000000000002"
ADAPTER = "0xf000000000000000000000000000000000000003"
CAST_VALUATION_DATA_FIXTURE = (
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000002"
    "0000000000000000000000000000000000000000000000000000000000000003"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "0000000000000000000000000000000000000000000000000000000000000005"
    "0000000000000000000000000000000000000000000000000000000000000006"
    "00000000000000000000000000000000000000000000000000000000000000e0"
    "0000000000000000000000000000000000000000000000000000000000000002"
    "1234000000000000000000000000000000000000000000000000000000000000"
)


class Repository:
    def __init__(self, missing_role=None, version=1):
        self.registry = {
            "chain_id": 84532,
            "fund_address": FUND,
            "deployment_status": "DEPLOYED",
            "enabled": True,
        }
        self.fund_state = {"as_of_block": 100, "reconciled": True}
        self.bindings = [
            {
                "contract_role": role,
                "contract_address": FUND,
                "interface_version": version,
                "implementation_address": FUND if role in PROXY_ROLES else None,
                "valid_from_block": 1,
                "valid_to_block": None,
            }
            for role in REQUIRED_TRUSTED_ROLES
            if role != missing_role
        ]
        self.blocked = []

    def enabled_funds(self):
        return [self.registry]

    def state(self, _chain, _fund):
        return self.fund_state

    def contracts(self, _chain, _fund):
        return self.bindings

    def record_blocked(self, _fund, reason):
        self.blocked.append(reason)
        return ReportRun(status="blocked", reason_code=reason)


def test_registry_loader_requires_complete_supported_bindings() -> None:
    trusted = TrustedRegistryLoader(Repository()).load()[0]
    missing = TrustedRegistryLoader(Repository(missing_role="nav_verifier")).load()[0]
    unsupported = TrustedRegistryLoader(Repository(version=2)).load()[0]

    assert trusted.trust_reason is None
    assert missing.trust_reason == "MISSING_TRUSTED_DEPLOYMENT"
    assert unsupported.trust_reason == "UNSUPPORTED_INTERFACE"


@pytest.mark.asyncio
async def test_reporter_loop_reloads_indexed_state_each_cycle(monkeypatch) -> None:
    built = []
    sleeps = 0

    class Reporter:
        def __init__(self, snapshot_block):
            self.snapshot_block = snapshot_block

        def run_once(self):
            return ReportRun(status="confirmed", reason_code=str(self.snapshot_block))

    def build_reporter():
        reporter = Reporter(len(built) + 100)
        built.append(reporter.snapshot_block)
        return reporter

    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(runtime, "build_reporter", build_reporter)
    monkeypatch.setattr(fund_nav_reporter.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await fund_nav_reporter.run()

    assert built == [100, 101]


def test_blocked_and_runtime_reporters_record_without_rpc_send() -> None:
    repository = Repository(missing_role="nav_verifier")
    fund = TrustedRegistryLoader(repository).load()[0]
    blocked = BlockedReporter(repository, fund, fund.trust_reason)

    assert blocked.run_once().reason_code == "MISSING_TRUSTED_DEPLOYMENT"
    assert repository.blocked == ["MISSING_TRUSTED_DEPLOYMENT"]

    inner = SimpleNamespace(
        run_once=lambda: (_ for _ in ()).throw(
            RuntimeError("INCOMPLETE_OBSERVER_QUORUM")
        )
    )
    runtime = RuntimeReporter(inner, repository, fund)
    assert runtime.run_once().reason_code == "INCOMPLETE_OBSERVER_QUORUM"


def test_concrete_gateway_simulation_calls_and_estimates_without_send() -> None:
    calls = []

    class Function:
        def call(self, transaction, block_identifier):
            calls.append(("call", transaction, block_identifier))

        def estimate_gas(self, transaction, block_identifier):
            calls.append(("estimate", transaction, block_identifier))
            return 100

    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway._submit_function = lambda *_: Function()

    gateway.simulate(
        report_nonce=1,
        reports=[],
        reporters=[],
        signatures=[],
        sender=FUND,
    )

    assert calls == [
        ("call", {"from": Web3.to_checksum_address(FUND)}, "pending"),
        ("estimate", {"from": Web3.to_checksum_address(FUND)}, "pending"),
    ]


@pytest.mark.parametrize("message", ["already known", "Already Imported"])
def test_rebroadcast_treats_known_transaction_as_success(message) -> None:
    class Eth:
        def send_raw_transaction(self, _raw):
            raise ValueError(message)

    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.w3 = SimpleNamespace(eth=Eth())
    transaction = SignedTransaction(Web3.to_hex(Web3.keccak(b"raw")), b"raw")

    assert gateway.broadcast(transaction) == transaction.transaction_hash


def test_valuation_data_encoding_matches_cast_outer_tuple_fixture() -> None:
    observation = OptionObservation(
        chain_id=84532,
        fund_address=FUND,
        valuator_address=VALUATOR,
        adapter_address=ADAPTER,
        position_id=1,
        snapshot_block=2,
        snapshot_block_hash="0x" + "00" * 32,
        valid_until_block=3,
        liability=4,
        base_exit_cost=5,
        observation_nonce=6,
        signature="0x1234",
    )

    assert encode_valuation_data([observation]).hex() == CAST_VALUATION_DATA_FIXTURE


def test_concrete_gateway_requires_independent_exact_observer_quorum() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    observers = tuple(Account.from_key(key).address.lower() for key in keys)
    digest = Web3.keccak(text="observation")

    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, block_identifier):
            assert block_identifier == 100
            return self.value

    class Functions:
        def observationDigest(self, *_args):
            return Call(digest)

        def isApprovedObserver(self, _observer):
            return Call(True)

    valuator = SimpleNamespace(
        address=Web3.to_checksum_address(VALUATOR), functions=Functions()
    )
    fund = TrustedFund({"chain_id": 84532, "fund_address": FUND}, {}, {}, None)
    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.fund = fund
    gateway.block_hash = lambda _block: bytes.fromhex("12" * 32)
    gateway.head_block = lambda: 101

    rows = [
        {
            "chain_id": 84532,
            "fund_address": FUND,
            "valuator_address": VALUATOR,
            "adapter_address": ADAPTER,
            "position_id": "1",
            "snapshot_block": 100,
            "snapshot_block_hash": "0x" + "12" * 32,
            "valid_until_block": 110,
            "liability": "20",
            "base_exit_cost": "2",
            "observation_nonce": str(index + 1),
            "signature": Web3.to_hex(sign_digest(digest, key)),
            "digest": Web3.to_hex(digest),
            "observer_address": observers[index],
            "market_maker_address": observers[0],
        }
        for index, key in enumerate(keys)
    ]

    accepted = gateway._position_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        position_id=1,
        market_maker=observers[0],
        rows=rows,
        quorum=2,
    )
    assert len(accepted) == 2

    with pytest.raises(RuntimeError, match="INCOMPLETE_OBSERVER_QUORUM"):
        gateway._position_observations(
            valuator=valuator,
            adapter=ADAPTER,
            block=100,
            position_id=1,
            market_maker=observers[0],
            rows=rows[:1],
            quorum=1,
        )
