import asyncio
from types import SimpleNamespace

import pytest
from eth_account import Account
from web3 import Web3

from src.bots import fund_nav_reporter
from src.config import get_fund_csp_sepolia_observer_private_keys, settings
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


def test_observation_chain_normalizes_stored_addresses_for_web3() -> None:
    seen = []

    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, block_identifier):
            assert block_identifier == 100
            return self.value

    class Functions:
        def observationDigest(self, adapter, *_args):
            assert Web3.is_checksum_address(adapter)
            return Call(bytes.fromhex("34" * 32))

        def isApprovedObserver(self, observer):
            assert Web3.is_checksum_address(observer)
            return Call(True)

        def position(self, _position_id):
            return Call((FUND, ADAPTER))

        def maxObservationWindow(self):
            return Call(120)

    class Eth:
        def contract(self, address, abi):
            assert Web3.is_checksum_address(address)
            seen.append(address)
            return SimpleNamespace(functions=Functions())

    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.w3 = SimpleNamespace(eth=Eth())
    observation = OptionObservation(
        chain_id=84532,
        fund_address=FUND,
        valuator_address=VALUATOR.lower(),
        adapter_address=ADAPTER.lower(),
        position_id=1,
        snapshot_block=100,
        snapshot_block_hash="0x" + "12" * 32,
        valid_until_block=110,
        liability=25,
        base_exit_cost=0,
        observation_nonce=1,
        signature="0x" + "00" * 65,
    )

    assert gateway.observation_digest(observation) == bytes.fromhex("34" * 32)
    assert gateway.observer_approved(VALUATOR.lower(), FUND, 100) is True
    assert gateway.market_maker(ADAPTER.lower(), 1, 100) == ADAPTER
    assert gateway.max_observation_window(VALUATOR.lower(), 100) == 120
    assert seen == [
        Web3.to_checksum_address(VALUATOR),
        Web3.to_checksum_address(VALUATOR),
        Web3.to_checksum_address(ADAPTER),
        Web3.to_checksum_address(VALUATOR),
    ]


class ConservativeObservationRepository:
    def __init__(self):
        self.rows = []

    def insert_verified_idempotent(self, row):
        identity = (
            row["chain_id"],
            row["valuator_address"],
            row["adapter_address"],
            row["position_id"],
            row["snapshot_block"],
            row["observer_address"],
        )
        if not any(
            (
                item["chain_id"],
                item["valuator_address"],
                item["adapter_address"],
                item["position_id"],
                item["snapshot_block"],
                item["observer_address"],
            )
            == identity
            for item in self.rows
        ):
            self.rows.append(row)


def conservative_gateway(keys):
    market_maker = Account.from_key("0x" + f"{3:064x}").address.lower()
    repository = ConservativeObservationRepository()
    fund = TrustedFund({"chain_id": 84532, "fund_address": FUND}, {}, {}, None)
    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.fund = fund
    gateway.repository = repository
    gateway.sepolia_observer_private_keys = keys
    gateway.chain_id = lambda: 84532
    gateway.block_hash = lambda _block: bytes.fromhex("12" * 32)
    gateway.head_block = lambda: 101
    gateway.max_observation_window = lambda _valuator, _block: 20
    gateway.observer_approved = lambda _valuator, _observer, _block: True
    gateway.market_maker = lambda _adapter, _position, _block: market_maker
    gateway.observation_digest = lambda observation: Web3.keccak(
        text=f"{observation.position_id}:{observation.observation_nonce}"
    )
    position = (
        FUND,
        market_maker,
        1,
        1,
        25,
        0,
        0,
        0,
        0,
        0,
        0,
        1,
        bytes(32),
    )
    return gateway, repository, position


def test_sepolia_conservative_observations_are_exact_and_idempotent() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, repository, position = conservative_gateway(keys)
    valuator = SimpleNamespace(address=Web3.to_checksum_address(VALUATOR))

    gateway._publish_sepolia_conservative_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        quorum=2,
        positions=[(1, position)],
        existing_rows=[],
    )
    gateway._publish_sepolia_conservative_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        quorum=2,
        positions=[(1, position)],
        existing_rows=repository.rows,
    )

    assert len(repository.rows) == 2
    assert {int(row["liability"]) for row in repository.rows} == {25}
    assert {int(row["base_exit_cost"]) for row in repository.rows} == {0}
    assert len({row["observation_nonce"] for row in repository.rows}) == 2
    assert {row["snapshot_block_hash"] for row in repository.rows} == {"0x" + "12" * 32}


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda gateway: setattr(gateway, "chain_id", lambda: 8453), "WRONG_CHAIN"),
        (
            lambda gateway: setattr(
                gateway, "sepolia_observer_private_keys", ("0x" + f"{1:064x}",)
            ),
            "QUORUM_MISMATCH",
        ),
        (
            lambda gateway: setattr(
                gateway,
                "observer_approved",
                lambda _valuator, _observer, _block: False,
            ),
            "NOT_APPROVED",
        ),
    ],
)
def test_sepolia_conservative_observations_fail_closed(mutation, reason) -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, _repository, position = conservative_gateway(keys)
    mutation(gateway)

    with pytest.raises(RuntimeError, match=reason):
        gateway._publish_sepolia_conservative_observations(
            valuator=SimpleNamespace(address=Web3.to_checksum_address(VALUATOR)),
            adapter=ADAPTER,
            block=100,
            quorum=2,
            positions=[(1, position)],
            existing_rows=[],
        )


def test_sepolia_conservative_observations_reject_policy_mismatch() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, _repository, position = conservative_gateway(keys)
    existing = [
        {
            "position_id": 1,
            "observer_address": Account.from_key(keys[0]).address.lower(),
            "liability": 24,
            "base_exit_cost": 0,
        }
    ]

    with pytest.raises(RuntimeError, match="POLICY_MISMATCH"):
        gateway._publish_sepolia_conservative_observations(
            valuator=SimpleNamespace(address=Web3.to_checksum_address(VALUATOR)),
            adapter=ADAPTER,
            block=100,
            quorum=2,
            positions=[(1, position)],
            existing_rows=existing,
        )


def test_sepolia_observer_key_config_is_disabled_and_strict(monkeypatch) -> None:
    assert settings.fund_csp_sepolia_conservative_observations_enabled is False
    key = "0x" + f"{1:064x}"
    monkeypatch.setattr(settings, "fund_csp_sepolia_observer_private_keys", key)
    with pytest.raises(ValueError, match="exactly two"):
        get_fund_csp_sepolia_observer_private_keys()

    monkeypatch.setattr(
        settings, "fund_csp_sepolia_observer_private_keys", f"{key},{key}"
    )
    with pytest.raises(ValueError, match="duplicate observers"):
        get_fund_csp_sepolia_observer_private_keys()
