import asyncio
import threading
from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_account import Account
from web3 import Web3

from src.bots import fund_nav_reporter
from src.config import (
    get_fund_covered_call_sepolia_fair_value_policy,
    get_fund_covered_call_sepolia_observer_private_keys,
    get_fund_csp_sepolia_fair_value_policy,
    get_fund_csp_sepolia_observer_private_keys,
    get_meta_wheel_nav_reporter_private_keys,
    get_meta_wheel_observer_private_keys,
    get_tokenized_fund_rpc_url,
    settings,
    validate_meta_wheel_credential_topology,
)
from src.fund_nav import runtime
from src.fund_nav.fair_value import (
    CoveredCallFairValuePolicy,
    FairValuePolicy,
    observation_model_version,
)
from src.fund_nav.fair_value import versioned_observation_nonce
from src.fund_nav.models import sign_digest
from src.fund_nav.observations import OptionObservation
from src.fund_nav.reporter import ReportRun, SignedTransaction
from src.fund_nav.reporter import (
    SUBMISSION_LEAD_BLOCKS,
    _submitter_transaction_lock,
)
from src.fund_nav.runtime import (
    BlockedReporter,
    ReporterFleet,
    RuntimeReporter,
    SupabaseNavRepository,
    TrustedFund,
    TrustedRegistryLoader,
    Web3ReporterGateway,
    encode_valuation_data,
    meta_wheel_producer_gate_reason,
    meta_wheel_reporting_gate_reason,
)
from src.vaults.csp_service import PROXY_ROLES, REQUIRED_TRUSTED_ROLES

FUND = "0xf000000000000000000000000000000000000001"
VALUATOR = "0xf000000000000000000000000000000000000002"
ADAPTER = "0xf000000000000000000000000000000000000003"
OTOKEN = "0xf000000000000000000000000000000000000004"
USDC = "0xf000000000000000000000000000000000000005"
WETH = "0xf000000000000000000000000000000000000006"
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


def test_tokenized_fund_rpc_prefers_dedicated_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rpc_url", "https://global-rpc.example")
    monkeypatch.setattr(
        settings,
        "tokenized_fund_rpc_url",
        "  https://fund-rpc.example  ",
    )

    assert get_tokenized_fund_rpc_url() == "https://fund-rpc.example"


def test_tokenized_fund_rpc_falls_back_to_global_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rpc_url", "  https://global-rpc.example  ")
    monkeypatch.setattr(settings, "tokenized_fund_rpc_url", "  ")

    assert get_tokenized_fund_rpc_url() == "https://global-rpc.example"


def test_meta_wheel_token_balance_checksums_registry_token_address() -> None:
    requested = []

    class Call:
        def call(self, block_identifier):
            assert block_identifier == 100
            return 42

    class Functions:
        def balanceOf(self, account):
            assert account == FUND
            return Call()

    class Eth:
        def contract(self, address, abi):
            requested.append(address)
            assert abi == runtime.ERC20_ABI
            return SimpleNamespace(functions=Functions())

    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.w3 = SimpleNamespace(eth=Eth())

    balance = gateway._token_balance(USDC.lower(), FUND, 100)

    assert balance == 42
    assert requested == [Web3.to_checksum_address(USDC)]


def test_runtime_reporter_constructs_provider_from_selected_fund_rpc(
    monkeypatch,
) -> None:
    provider_urls = []
    built_gateway = object()
    built_reporter = SimpleNamespace(run_once=lambda: ReportRun(status="confirmed"))

    def provider_factory(url, chain_id, name):
        provider_urls.append((url, chain_id, name))
        return SimpleNamespace(provider=("provider", url))

    fund = TrustedFund(
        registry={
            "chain_id": 84532,
            "fund_address": FUND,
            "strategy_kind": "csp",
        },
        state={},
        contracts={},
        trust_reason=None,
    )
    monkeypatch.setattr(
        runtime,
        "get_tokenized_fund_rpc_url",
        lambda: "https://fund-rpc.example",
    )
    monkeypatch.setattr(runtime, "create_validated_backend_w3", provider_factory)
    monkeypatch.setattr(
        runtime,
        "Web3ReporterGateway",
        lambda w3, *_args, **_kwargs: (
            built_gateway
            if w3.provider == ("provider", "https://fund-rpc.example")
            else pytest.fail("runtime reporter used the wrong RPC")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "NavReporter",
        lambda gateway, *_args, **_kwargs: (
            built_reporter
            if gateway is built_gateway
            else pytest.fail("unexpected gateway")
        ),
    )
    monkeypatch.setattr(
        runtime, "get_fund_nav_reporter_private_keys", lambda: ("reporter-key",)
    )
    monkeypatch.setattr(
        runtime, "get_fund_nav_submitter_private_key", lambda: "submitter-key"
    )
    monkeypatch.setattr(
        runtime.settings,
        "fund_csp_sepolia_fair_value_observations_enabled",
        False,
    )

    reporter = runtime._build_fund_reporter(Repository(), fund)

    assert reporter.reporter is built_reporter
    assert provider_urls == [
        ("https://fund-rpc.example", fund.chain_id, "TOKENIZED_FUND_RPC_URL")
    ]


def test_meta_wheel_reporting_gate_accepts_only_canonical_accounting_operator() -> None:
    private_key = "0x" + f"{73:064x}"
    registry = {
        "strategy_kind": "meta_wheel",
        "accounting_role_account": Account.from_key(private_key).address.lower(),
    }

    assert meta_wheel_reporting_gate_reason(registry, private_key) is None


@pytest.mark.parametrize(
    ("private_key", "accounting_role", "expected_reason"),
    [
        (
            "",
            Account.from_key("0x" + f"{74:064x}").address,
            "META_WHEEL_OPERATOR_KEY_MISSING",
        ),
        (
            "not-a-private-key",
            Account.from_key("0x" + f"{74:064x}").address,
            "META_WHEEL_OPERATOR_KEY_INVALID",
        ),
        ("0x" + f"{75:064x}", None, "META_WHEEL_ACCOUNTING_ROLE_MISSING"),
        ("0x" + f"{75:064x}", "not-an-address", "META_WHEEL_ACCOUNTING_ROLE_INVALID"),
        (
            "0x" + f"{75:064x}",
            Account.from_key("0x" + f"{76:064x}").address,
            "META_WHEEL_OPERATOR_ACCOUNTING_ROLE_MISMATCH",
        ),
    ],
)
def test_meta_wheel_reporting_gate_fails_closed_without_exposing_key(
    private_key,
    accounting_role,
    expected_reason,
) -> None:
    reason = meta_wheel_reporting_gate_reason(
        {
            "strategy_kind": "meta_wheel",
            "accounting_role_account": accounting_role,
        },
        private_key,
    )

    assert reason == expected_reason
    if private_key:
        assert private_key not in reason


@pytest.mark.parametrize("strategy_kind", ["csp", "covered_call"])
def test_standalone_reporting_does_not_depend_on_meta_wheel_operator_gate(
    strategy_kind,
) -> None:
    assert (
        meta_wheel_reporting_gate_reason(
            {"strategy_kind": strategy_kind},
            "not-a-private-key",
        )
        is None
    )


def test_meta_wheel_producer_requires_exact_configured_fund_key(monkeypatch) -> None:
    registry = {
        "strategy_kind": "meta_wheel",
        "fund_key": "base-sepolia:meta-wheel",
        "chain_id": 84532,
    }
    monkeypatch.setattr(settings, "meta_wheel_fund_key", "")
    assert meta_wheel_producer_gate_reason(registry) == "META_WHEEL_FUND_KEY_MISSING"

    monkeypatch.setattr(settings, "meta_wheel_fund_key", "another-fund")
    assert meta_wheel_producer_gate_reason(registry) == "META_WHEEL_FUND_KEY_MISMATCH"


def test_meta_wheel_producer_requires_both_child_observation_pipelines(
    monkeypatch,
) -> None:
    registry = {
        "strategy_kind": "meta_wheel",
        "fund_key": "base-sepolia:meta-wheel",
        "chain_id": 84532,
    }
    monkeypatch.setattr(settings, "meta_wheel_fund_key", registry["fund_key"])
    monkeypatch.setattr(
        settings,
        "meta_wheel_csp_sepolia_fair_value_observations_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "meta_wheel_covered_call_sepolia_fair_value_observations_enabled",
        False,
    )

    assert (
        meta_wheel_producer_gate_reason(registry)
        == "META_WHEEL_CHILD_OBSERVATIONS_DISABLED"
    )


@pytest.mark.parametrize("strategy_kind", ["csp", "covered_call"])
def test_standalone_reporting_does_not_depend_on_meta_wheel_producer_gate(
    strategy_kind,
) -> None:
    assert meta_wheel_producer_gate_reason({"strategy_kind": strategy_kind}) is None


def test_meta_wheel_operator_mismatch_blocks_before_rpc_construction(
    monkeypatch,
) -> None:
    configured_private_key = "0x" + f"{77:064x}"
    fund = TrustedFund(
        registry={
            "chain_id": 84532,
            "fund_address": FUND,
            "strategy_kind": "meta_wheel",
            "accounting_role_account": Account.from_key(
                "0x" + f"{78:064x}"
            ).address.lower(),
        },
        state={},
        contracts={},
        trust_reason=None,
    )
    monkeypatch.setattr(
        runtime.settings,
        "meta_wheel_operator_private_key",
        configured_private_key,
    )
    monkeypatch.setattr(
        runtime.Web3,
        "HTTPProvider",
        lambda _url: pytest.fail("RPC constructed before operator gate"),
    )

    reporter = runtime._build_fund_reporter(Repository(), fund)

    assert isinstance(reporter, BlockedReporter)
    assert reporter.reason == "META_WHEEL_OPERATOR_ACCOUNTING_ROLE_MISMATCH"


def test_meta_wheel_credentials_are_strict_and_separate(monkeypatch) -> None:
    keys = ["0x" + f"{value:064x}" for value in range(81, 88)]
    operator, reporter_a, reporter_b, csp_a, csp_b, call_a, call_b = keys
    monkeypatch.setattr(settings, "meta_wheel_operator_private_key", operator)
    monkeypatch.setattr(
        settings,
        "meta_wheel_nav_reporter_private_keys",
        f"{reporter_a},{reporter_b}",
    )
    monkeypatch.setattr(
        settings,
        "meta_wheel_csp_sepolia_observer_private_keys",
        f"{csp_a},{csp_b}",
    )
    monkeypatch.setattr(
        settings,
        "meta_wheel_covered_call_sepolia_observer_private_keys",
        f"{call_a},{call_b}",
    )
    for name in (
        "fund_nav_reporter_private_keys",
        "fund_nav_submitter_private_key",
        "fund_csp_sepolia_observer_private_keys",
        "fund_covered_call_sepolia_observer_private_keys",
    ):
        monkeypatch.setattr(settings, name, "")

    validate_meta_wheel_credential_topology()
    assert get_meta_wheel_nav_reporter_private_keys() == (reporter_a, reporter_b)
    assert get_meta_wheel_observer_private_keys("csp") == (csp_a, csp_b)
    assert get_meta_wheel_observer_private_keys("covered_call") == (call_a, call_b)


def test_meta_wheel_credentials_fail_closed(monkeypatch) -> None:
    key = "0x" + f"{84:064x}"
    monkeypatch.setattr(settings, "meta_wheel_nav_reporter_private_keys", key)
    monkeypatch.setattr(
        settings, "meta_wheel_csp_sepolia_observer_private_keys", f"{key},{key}"
    )

    with pytest.raises(ValueError, match="exactly two"):
        get_meta_wheel_nav_reporter_private_keys()
    with pytest.raises(ValueError, match="duplicate observers"):
        get_meta_wheel_observer_private_keys("csp")


@pytest.mark.parametrize(
    ("overlap_target", "message"),
    [
        ("reporter", "operator.*NAV reporters"),
        ("csp", "NAV reporters.*CSP observers"),
        ("call", "CSP observers.*covered-call observers"),
        ("standalone", "Meta Wheel NAV reporters.*standalone NAV reporters"),
    ],
)
def test_meta_wheel_credential_topology_rejects_cross_domain_overlap(
    monkeypatch, overlap_target, message
) -> None:
    keys = ["0x" + f"{value:064x}" for value in range(91, 99)]
    operator, reporter_a, reporter_b, csp_a, csp_b, call_a, call_b, standalone = keys
    if overlap_target == "reporter":
        reporter_a = operator
    elif overlap_target == "csp":
        csp_a = reporter_a
    elif overlap_target == "call":
        call_a = csp_a
    elif overlap_target == "standalone":
        standalone = reporter_a
    monkeypatch.setattr(settings, "meta_wheel_operator_private_key", operator)
    monkeypatch.setattr(
        settings, "meta_wheel_nav_reporter_private_keys", f"{reporter_a},{reporter_b}"
    )
    monkeypatch.setattr(
        settings, "meta_wheel_csp_sepolia_observer_private_keys", f"{csp_a},{csp_b}"
    )
    monkeypatch.setattr(
        settings,
        "meta_wheel_covered_call_sepolia_observer_private_keys",
        f"{call_a},{call_b}",
    )
    monkeypatch.setattr(settings, "fund_nav_reporter_private_keys", standalone)
    monkeypatch.setattr(settings, "fund_nav_submitter_private_key", "")
    monkeypatch.setattr(settings, "fund_csp_sepolia_observer_private_keys", "")
    monkeypatch.setattr(settings, "fund_covered_call_sepolia_observer_private_keys", "")

    with pytest.raises(ValueError, match=message):
        validate_meta_wheel_credential_topology()


def test_unknown_strategy_kind_blocks_before_rpc_or_credentials(monkeypatch) -> None:
    fund = TrustedFund(
        registry={"strategy_kind": "meta-wheel-typo"},
        state={},
        contracts={},
        trust_reason=None,
    )
    monkeypatch.setattr(
        runtime.Web3,
        "HTTPProvider",
        lambda *_: pytest.fail("RPC constructed for unsupported strategy"),
    )
    monkeypatch.setattr(
        runtime,
        "get_fund_nav_reporter_private_keys",
        lambda: pytest.fail("credentials selected for unsupported strategy"),
    )

    reporter = runtime._build_fund_reporter(Repository(), fund)

    assert isinstance(reporter, BlockedReporter)
    assert reporter.reason == "UNSUPPORTED_STRATEGY_KIND"


def test_meta_wheel_reporter_uses_only_wheel_credentials(monkeypatch) -> None:
    captured = {}
    fund = TrustedFund(
        registry={
            "chain_id": 84532,
            "fund_address": FUND,
            "fund_key": "base-sepolia:meta-wheel",
            "strategy_kind": "meta_wheel",
        },
        state={},
        contracts={},
        trust_reason=None,
    )
    monkeypatch.setattr(runtime, "meta_wheel_reporting_gate_reason", lambda *_: None)
    monkeypatch.setattr(runtime, "meta_wheel_producer_gate_reason", lambda *_: None)
    monkeypatch.setattr(runtime, "get_tokenized_fund_rpc_url", lambda: "http://rpc")

    monkeypatch.setattr(
        runtime,
        "create_validated_backend_w3",
        lambda url, *_args: SimpleNamespace(provider=url),
    )
    monkeypatch.setattr(
        runtime,
        "get_meta_wheel_observer_private_keys",
        lambda kind: (f"{kind}-observer-a", f"{kind}-observer-b"),
    )
    monkeypatch.setattr(
        runtime,
        "get_meta_wheel_nav_reporter_private_keys",
        lambda: ("wheel-a", "wheel-b"),
    )
    monkeypatch.setattr(
        runtime.settings, "meta_wheel_operator_private_key", "accounting"
    )
    monkeypatch.setattr(
        runtime,
        "get_fund_nav_reporter_private_keys",
        lambda: pytest.fail("standalone reporters selected for Meta Wheel"),
    )
    monkeypatch.setattr(
        runtime,
        "get_fund_nav_submitter_private_key",
        lambda: pytest.fail("standalone submitter selected for Meta Wheel"),
    )
    monkeypatch.setattr(
        runtime, "get_fund_csp_sepolia_fair_value_policy", lambda: "csp-policy"
    )
    monkeypatch.setattr(
        runtime,
        "get_fund_covered_call_sepolia_fair_value_policy",
        lambda: "call-policy",
    )

    def gateway(_w3, _fund, _repository, **kwargs):
        captured["contexts"] = kwargs["wheel_valuation_contexts"]
        return object()

    def reporter(_gateway, _store, **kwargs):
        captured["reporters"] = kwargs["private_keys"]
        captured["submitter"] = kwargs["submitter_private_key"]
        return SimpleNamespace(run_once=lambda: ReportRun(status="confirmed"))

    monkeypatch.setattr(runtime, "Web3ReporterGateway", gateway)
    monkeypatch.setattr(runtime, "NavReporter", reporter)

    built = runtime._build_fund_reporter(Repository(), fund)

    assert isinstance(built, RuntimeReporter)
    assert captured["reporters"] == ("wheel-a", "wheel-b")
    assert captured["submitter"] == "accounting"
    assert captured["contexts"]["csp"].observer_private_keys == (
        "csp-observer-a",
        "csp-observer-b",
    )
    assert captured["contexts"]["covered_call"].observer_private_keys == (
        "covered_call-observer-a",
        "covered_call-observer-b",
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


def test_gateway_detects_when_signed_transaction_nonce_was_consumed() -> None:
    account = Account.from_key("0x" + f"{99:064x}")
    signed = account.sign_transaction(
        {
            "type": 2,
            "chainId": 84532,
            "nonce": 7,
            "to": Web3.to_checksum_address(FUND),
            "value": 0,
            "gas": 21_000,
            "maxFeePerGas": 30_000_000,
            "maxPriorityFeePerGas": 1_000_000,
        }
    )
    gateway = object.__new__(Web3ReporterGateway)
    gateway.w3 = SimpleNamespace(
        eth=SimpleNamespace(get_transaction_count=lambda _sender, _tag: 8)
    )

    assert gateway.transaction_nonce_consumed(Web3.to_hex(signed.raw_transaction))

    gateway.w3.eth.get_transaction_count = lambda _sender, _tag: 7
    assert not gateway.transaction_nonce_consumed(Web3.to_hex(signed.raw_transaction))


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


@pytest.mark.asyncio
async def test_reporter_loop_backoff_grows_caps_and_resets(monkeypatch) -> None:
    statuses = iter(["failed", "failed", "confirmed", "failed", "failed"])
    delays = []

    class Reporter:
        def run_once(self):
            return ReportRun(status=next(statuses))

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(runtime, "build_reporter", Reporter)
    monkeypatch.setattr(settings, "fund_nav_reporter_interval_seconds", 200)
    monkeypatch.setattr(fund_nav_reporter.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await fund_nav_reporter.run()

    assert delays == [200, 300, 200, 200, 300]


@pytest.mark.asyncio
async def test_reporter_fleet_worker_backoff_resets_after_success(monkeypatch) -> None:
    statuses = iter(["failed", "failed", "confirmed", "failed"])
    delays = []

    class Reporter:
        def run_once(self):
            return ReportRun(status=next(statuses))

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await ReporterFleet([Reporter]).run_forever(40, lambda _result: None)

    assert delays == [40, 80, 40, 40]


def test_nav_registry_queries_use_explicit_minimal_columns() -> None:
    calls = []

    class Query:
        def __init__(self, table):
            self.table_name = table
            self.data = []

        def select(self, columns):
            calls.append((self.table_name, "select", columns))
            return self

        def eq(self, column, value):
            calls.append((self.table_name, "eq", column, value))
            return self

        def execute(self):
            return self

    class Client:
        postgrest = SimpleNamespace(aclose=lambda: None)

        def table(self, name):
            return Query(name)

    repository = SupabaseNavRepository(Client())

    assert repository.enabled_funds() == []
    assert repository.contracts(84532, FUND) == []
    assert ("v2_fund_registry", "select", runtime.FUND_REGISTRY_COLUMNS) in calls
    assert ("v2_fund_contracts", "select", runtime.FUND_CONTRACT_COLUMNS) in calls
    assert not any(
        call[:2] == ("v2_fund_registry", "select") and call[2] == "*" for call in calls
    )


@pytest.mark.asyncio
async def test_reporter_fleet_workers_do_not_barrier_between_cycles(
    monkeypatch,
) -> None:
    slow_release = threading.Event()
    fast_runs = 0
    built = {"slow": 0, "fast": 0}
    real_sleep = asyncio.sleep

    class SlowReporter:
        def run_once(self):
            slow_release.wait(timeout=1)
            return ReportRun(status="confirmed")

    class FastReporter:
        def run_once(self):
            nonlocal fast_runs
            fast_runs += 1
            if fast_runs == 3:
                slow_release.set()
            return ReportRun(status="confirmed")

    def factory(name, reporter):
        def build():
            built[name] += 1
            return reporter()

        return build

    async def stop_after_three_fast_runs(_seconds):
        if fast_runs >= 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    fleet = ReporterFleet(
        [
            factory("slow", SlowReporter),
            factory("fast", FastReporter),
        ]
    )
    monkeypatch.setattr(runtime.asyncio, "sleep", stop_after_three_fast_runs)

    with pytest.raises(asyncio.CancelledError):
        await fleet.run_forever(1, lambda _result: None)

    assert fast_runs >= 3
    assert built["fast"] >= 3
    assert built["slow"] == 1


@pytest.mark.asyncio
async def test_reporter_fleet_worker_failure_isolated_and_retried(monkeypatch) -> None:
    attempts = {"failed": 0, "healthy": 0}
    real_sleep = asyncio.sleep

    class HealthyReporter:
        def run_once(self):
            attempts["healthy"] += 1
            return ReportRun(status="confirmed")

    def failed_factory():
        attempts["failed"] += 1
        raise RuntimeError("isolated factory failure")

    async def stop_after_retries(_seconds):
        if attempts["failed"] >= 2 and attempts["healthy"] >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(runtime.asyncio, "sleep", stop_after_retries)
    results = []
    fleet = ReporterFleet([failed_factory, HealthyReporter])

    with pytest.raises(asyncio.CancelledError):
        await fleet.run_forever(1, results.append)

    assert attempts["failed"] >= 2
    assert attempts["healthy"] >= 2
    assert any(result.reason_code == "REPORT_BUILD_FAILED" for result in results)
    assert any(result.status == "confirmed" for result in results)


@pytest.mark.asyncio
async def test_reporter_fleet_cancellation_keeps_event_loop_responsive() -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowReporter:
        def run_once(self):
            started.set()
            release.wait(timeout=1)
            return ReportRun(status="confirmed")

    fleet = ReporterFleet([SlowReporter])
    task = asyncio.create_task(fleet.run_forever(1, lambda _result: None))
    while not started.is_set():
        await asyncio.sleep(0)

    task.cancel()

    async def release_worker():
        await asyncio.sleep(0.01)
        release.set()

    release_task = asyncio.create_task(release_worker())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)
    await release_task


def test_reporter_fleet_runs_fund_waits_concurrently() -> None:
    barrier = threading.Barrier(2)
    loaded = []
    loaded_lock = threading.Lock()

    class Reporter:
        def __init__(self, name):
            self.name = name

        def run_once(self):
            barrier.wait(timeout=1)
            return ReportRun(status="confirmed")

    def factory(name):
        def build():
            with loaded_lock:
                loaded.append((name, threading.get_ident()))
            return Reporter(name)

        return build

    result = ReporterFleet([factory("first"), factory("second")]).run_once()

    assert result.status == "confirmed"
    assert {name for name, _thread in loaded} == {"first", "second"}
    assert len({thread for _name, thread in loaded}) == 2


def test_reporter_fleet_exception_does_not_cancel_other_fund() -> None:
    completed = threading.Event()

    class Reporter:
        def run_once(self):
            completed.set()
            return ReportRun(status="confirmed")

    def failed_factory():
        raise RuntimeError("isolated factory failure")

    result = ReporterFleet([failed_factory, Reporter]).run_once()

    assert completed.is_set()
    assert result.status == "failed"
    assert result.reason_code == "REPORT_BUILD_FAILED"


def test_reporter_fleet_cleanup_failure_does_not_replace_confirmed_run(
    caplog,
) -> None:
    class Reporter:
        def run_once(self):
            return ReportRun(status="confirmed")

        def close(self):
            raise RuntimeError("client close failed")

    result = ReporterFleet([Reporter, Reporter]).run_once()

    assert result.status == "confirmed"
    assert caplog.messages.count("Fund NAV reporter client cleanup failed") == 2


def test_testnet_execution_buffer_covers_observed_renewal_latency() -> None:
    previous_valid_until = 377
    next_snapshot = 337
    next_head_after_valuation = 369
    inclusion_margin = 3
    execution_buffer = 5
    activation_delay = 1
    next_valid_after = max(
        next_head_after_valuation + inclusion_margin + execution_buffer,
        next_snapshot + activation_delay,
    )

    assert execution_buffer > SUBMISSION_LEAD_BLOCKS
    assert next_valid_after <= previous_valid_until + 1


def test_submitter_lock_is_keyed_by_chain_and_sender() -> None:
    sender = "0x0000000000000000000000000000000000000001"

    first = _submitter_transaction_lock(84532, sender)

    assert first is _submitter_transaction_lock(84532, sender.upper())
    assert first is not _submitter_transaction_lock(1, sender)


def test_registered_factories_bound_client_lifetime_per_cycle(monkeypatch) -> None:
    barrier = threading.Barrier(2)
    created = []
    closed = []
    created_lock = threading.Lock()

    class Repository:
        def __init__(self, client, *, owns_client):
            self.client = client
            self.owns_client = owns_client
            with created_lock:
                created.append((threading.get_ident(), client))

        def close(self):
            assert self.owns_client
            with created_lock:
                closed.append(self.client)

    class Loader:
        def __init__(self, repository):
            self.repository = repository

        def load_one(self, registry):
            return registry

    class Reporter:
        def __init__(self, repository):
            self.repository = repository

        def run_once(self):
            barrier.wait(timeout=1)
            return ReportRun(status="confirmed")

        def close(self):
            self.repository.close()

    monkeypatch.setattr(runtime, "create_client", lambda _url, _key: object())
    monkeypatch.setattr(runtime, "SupabaseNavRepository", Repository)
    monkeypatch.setattr(runtime, "TrustedRegistryLoader", Loader)
    monkeypatch.setattr(
        runtime,
        "_build_fund_reporter",
        lambda repository, _fund: Reporter(repository),
    )

    factories = [
        lambda: runtime._build_registered_fund_reporter({"fund": "first"}),
        lambda: runtime._build_registered_fund_reporter({"fund": "second"}),
    ]
    first = ReporterFleet(factories).run_once()
    second = ReporterFleet(factories).run_once()

    assert first.status == second.status == "confirmed"
    assert len(created) == 4
    assert len({client for _thread, client in created}) == 4
    assert set(closed) == {client for _thread, client in created}


def test_owned_repository_client_closes_exactly_once() -> None:
    calls = []
    client = SimpleNamespace(
        postgrest=SimpleNamespace(aclose=lambda: calls.append("closed"))
    )
    repository = SupabaseNavRepository(client, owns_client=True)

    repository.close()
    repository.close()

    assert calls == ["closed"]


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


def test_gateway_accepts_only_fully_empty_unsynced_strategy_bootstrap() -> None:
    empty_adapter_state = (
        0,
        Web3.keccak(text="initial positions accumulator"),
        0,
        0,
        0,
        0,
        0,
    )
    live_empty_hash = Web3.keccak(text="empty covered-call adapter state")

    assert Web3ReporterGateway._strategy_state_matches(
        adapter_state=empty_adapter_state,
        observed_hash=live_empty_hash,
        component_nonce=0,
        component_hash=bytes(32),
    )
    assert not Web3ReporterGateway._strategy_state_matches(
        adapter_state=(*empty_adapter_state[:-1], 1),
        observed_hash=live_empty_hash,
        component_nonce=0,
        component_hash=bytes(32),
    )
    assert not Web3ReporterGateway._strategy_state_matches(
        adapter_state=(1, bytes(32), 0, 0, 0, 0, 0),
        observed_hash=live_empty_hash,
        component_nonce=0,
        component_hash=bytes(32),
    )


def test_gateway_requires_exact_hash_after_first_strategy_transition() -> None:
    committed_hash = Web3.keccak(text="committed strategy state")
    adapter_state = (1, Web3.keccak(text="positions"), 1, 1, 10, 10, 0)

    assert Web3ReporterGateway._strategy_state_matches(
        adapter_state=adapter_state,
        observed_hash=committed_hash,
        component_nonce=1,
        component_hash=committed_hash,
    )
    assert not Web3ReporterGateway._strategy_state_matches(
        adapter_state=adapter_state,
        observed_hash=Web3.keccak(text="different live state"),
        component_nonce=1,
        component_hash=committed_hash,
    )


def test_meta_wheel_uses_manager_nonce_and_position_hash_domains_separately() -> None:
    committed_hash = Web3.keccak(text="managed wheel position")
    coordinator_summary = (3, 2, 1, 500, 100, 90, 2, 600, 2)

    assert Web3ReporterGateway._strategy_state_matches(
        adapter_state=coordinator_summary,
        observed_hash=committed_hash,
        component_nonce=7,
        component_hash=committed_hash,
        compare_adapter_nonce=False,
    )
    assert not Web3ReporterGateway._strategy_state_matches(
        adapter_state=coordinator_summary,
        observed_hash=Web3.keccak(text="balance-sensitive mismatch"),
        component_nonce=7,
        component_hash=committed_hash,
        compare_adapter_nonce=False,
    )


def test_meta_wheel_parent_valuation_uses_extended_valuator_abi() -> None:
    requested_abis = []

    class Call:
        def call(self, block_identifier):
            assert block_identifier == 100
            return bytes.fromhex("11" * 32)

    class AdapterFunctions:
        def summary(self):
            return SimpleNamespace(
                call=lambda block_identifier: (8, 0, 0, 0, 0, 0, 0, 0)
            )

        def positionStateHash(self):
            return Call()

    class Eth:
        def contract(self, address, abi):
            requested_abis.append(abi)
            if address == ADAPTER:
                return SimpleNamespace(functions=AdapterFunctions())
            raise RuntimeError("VALUATOR_CONSTRUCTED")

    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.w3 = SimpleNamespace(eth=Eth())
    gateway.strategy_kind = "meta_wheel"
    gateway.adapter_role = "wheel_coordinator"
    gateway.valuator_role = "meta_wheel_valuator"
    gateway.adapter_abi = runtime.WHEEL_COORDINATOR_ABI
    gateway.addresses = {
        "wheel_coordinator": ADAPTER,
        "meta_wheel_valuator": VALUATOR,
    }

    with pytest.raises(RuntimeError, match="VALUATOR_CONSTRUCTED"):
        gateway._value_strategy(
            component_id=bytes(32),
            nonce=1,
            state_hash=bytes.fromhex("11" * 32),
            block=100,
            block_hash=bytes.fromhex("22" * 32),
        )

    assert requested_abis[-1] == runtime.META_WHEEL_VALUATOR_ABI


class _BlockValueCall:
    def __init__(self, values):
        self.values = values

    def call(self, block_identifier):
        return self.values[block_identifier]


class _StaticBlockCall:
    def __init__(self, value):
        self.value = value

    def call(self, *, block_identifier):
        assert block_identifier == 100
        return self.value


def _wheel_lane_gateway(*, row_hash: str, chain_hash: bytes):
    row = {
        "child_vault": ADAPTER,
        "strategy_kind": "csp",
        "custody_domain": ADAPTER,
        "snapshot_block": 100,
        "snapshot_block_hash": "0x" + "12" * 32,
        "valid_after_block": 95,
        "valid_until_block": 105,
        "child_shares": "100",
        "position_state_hash": row_hash,
        "gross_assets_usdc": "1000",
        "liabilities_usdc": "100",
        "liquid_usdc": "500",
        "base_exit_cost_usdc": "10",
        "data_hash": "0x" + "34" * 32,
        "valuation_data": "0x1234",
    }
    lane = SimpleNamespace(
        functions=SimpleNamespace(
            childShares=lambda: _StaticBlockCall(100),
            positionStateHash=lambda: _StaticBlockCall(chain_hash),
        )
    )
    gateway = object.__new__(Web3ReporterGateway)
    gateway.fund = SimpleNamespace(chain_id=84532, address=FUND)
    gateway.repository = SimpleNamespace(wheel_lane_valuations=lambda *_args: [row])
    gateway._produce_wheel_lane_valuations = lambda *_args: None
    gateway.w3 = SimpleNamespace(eth=SimpleNamespace(contract=lambda **_kwargs: lane))
    return gateway


def test_meta_wheel_blocks_lane_valuation_hash_not_bound_to_safe_block() -> None:
    gateway = _wheel_lane_gateway(
        row_hash="0x" + "56" * 32,
        chain_hash=bytes.fromhex("78" * 32),
    )

    with pytest.raises(RuntimeError, match="INCOHERENT_CHILD_NAV"):
        gateway._wheel_lane_valuations(100, bytes.fromhex("12" * 32))


def test_meta_wheel_persists_independent_safe_block_lane_hash_binding() -> None:
    chain_hash = bytes.fromhex("78" * 32)
    gateway = _wheel_lane_gateway(
        row_hash=Web3.to_hex(chain_hash),
        chain_hash=chain_hash,
    )

    reports = gateway._wheel_lane_valuations(100, bytes.fromhex("12" * 32))

    assert reports[0].position_state_hash == Web3.to_hex(chain_hash)
    assert reports[0].expected_position_state_hash == Web3.to_hex(chain_hash)


def test_meta_wheel_decodes_batched_child_shares() -> None:
    gateway = object.__new__(Web3ReporterGateway)
    gateway._wheel_multicall = lambda calls, block: [
        (True, encode(["uint256"], [index + 1])) for index, _ in enumerate(calls)
    ]
    functions = SimpleNamespace(
        childShares=lambda: SimpleNamespace(_encode_transaction_data=lambda: "0x1234")
    )
    contracts = iter(
        [
            SimpleNamespace(
                address=Web3.to_checksum_address(ADAPTER), functions=functions
            ),
            SimpleNamespace(
                address=Web3.to_checksum_address(OTOKEN), functions=functions
            ),
        ]
    )
    gateway.w3 = SimpleNamespace(
        eth=SimpleNamespace(contract=lambda **_kwargs: next(contracts))
    )

    shares = gateway._wheel_child_shares([ADAPTER, OTOKEN], 100)

    assert shares == {ADAPTER.lower(): 1, OTOKEN.lower(): 2}


def test_meta_wheel_batched_child_share_failure_is_fail_closed() -> None:
    gateway = object.__new__(Web3ReporterGateway)
    gateway._wheel_multicall = lambda _calls, _block: [(False, b"")]
    gateway.w3 = SimpleNamespace(
        eth=SimpleNamespace(
            contract=lambda **_kwargs: SimpleNamespace(
                address=Web3.to_checksum_address(ADAPTER),
                functions=SimpleNamespace(
                    childShares=lambda: SimpleNamespace(
                        _encode_transaction_data=lambda: "0x1234"
                    )
                ),
            )
        )
    )

    with pytest.raises(RuntimeError, match="META_WHEEL_LANE_DISCOVERY_FAILED"):
        gateway._wheel_child_shares([ADAPTER], 100)


class _SnapshotVaultFunctions:
    def __init__(self, *, flow_nonces, idle_hashes):
        self.flow_nonces = flow_nonces
        self.idle_hashes = idle_hashes

    def fundFlowNonce(self):
        return _BlockValueCall(self.flow_nonces)

    def idleStateHash(self):
        return _BlockValueCall(self.idle_hashes)


def _gateway_before_strategy_valuation(
    *,
    confirmed_flow_nonce=7,
    pending_flow_nonce=7,
    confirmed_component_nonce=3,
    pending_component_nonce=3,
):
    block = 100
    component_id = Web3.keccak(text="strategy component")
    idle_hash = Web3.keccak(text="idle state")
    component_hash = Web3.keccak(text="component state")
    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.fund = TrustedFund(
        {"chain_id": 84532, "fund_address": FUND},
        {
            "as_of_block": block,
            "as_of_block_hash": Web3.to_hex(Web3.keccak(text="block 100")),
            "reconciled": True,
        },
        {},
        None,
    )
    gateway.vault = SimpleNamespace(
        functions=_SnapshotVaultFunctions(
            flow_nonces={
                block: confirmed_flow_nonce,
                "pending": pending_flow_nonce,
            },
            idle_hashes={block: idle_hash, "pending": idle_hash},
        )
    )
    gateway._require_block_hash = lambda *_args: None
    gateway._validate_bindings = lambda *_args: None
    gateway._active_components = lambda _block: (component_id,)
    gateway._component_state = lambda _component_id, observed_block: (
        FUND,
        1,
        confirmed_component_nonce
        if observed_block == block
        else pending_component_nonce,
        component_hash,
        True,
    )
    return gateway


def test_snapshot_rejects_unindexed_idle_state_before_strategy_valuation() -> None:
    gateway = _gateway_before_strategy_valuation(
        confirmed_flow_nonce=7,
        pending_flow_nonce=8,
    )
    strategy_valuations = 0

    def value_strategy(*_args):
        nonlocal strategy_valuations
        strategy_valuations += 1
        return (), True

    gateway._strategy_reports = value_strategy

    with pytest.raises(RuntimeError, match="UNINDEXED_COMPONENT_STATE"):
        gateway.snapshot()

    assert strategy_valuations == 0


def test_snapshot_rejects_unindexed_strategy_state_before_valuation() -> None:
    gateway = _gateway_before_strategy_valuation(
        confirmed_component_nonce=3,
        pending_component_nonce=4,
    )
    strategy_valuations = 0

    def value_strategy(*_args):
        nonlocal strategy_valuations
        strategy_valuations += 1
        return (), True

    gateway._strategy_reports = value_strategy

    with pytest.raises(RuntimeError, match="UNINDEXED_COMPONENT_STATE"):
        gateway.snapshot()

    assert strategy_valuations == 0


def test_snapshot_starts_valuation_once_indexed_component_state_catches_up() -> None:
    gateway = _gateway_before_strategy_valuation()

    def value_strategy(*_args):
        raise RuntimeError("VALUATION_STARTED")

    gateway._strategy_reports = value_strategy

    with pytest.raises(RuntimeError, match="VALUATION_STARTED"):
        gateway.snapshot()


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

        def requiredModelVersion(self):
            return Call(1)

        def maxObservationDivergenceBps(self):
            return Call(500)

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
            "observation_nonce": str(versioned_observation_nonce(index + 1)),
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

    divergent_rows = [dict(row) for row in rows]
    divergent_rows[1]["liability"] = "22"
    with pytest.raises(RuntimeError, match="DIVERGENCE_EXCEEDED"):
        gateway._position_observations(
            valuator=valuator,
            adapter=ADAPTER,
            block=100,
            position_id=1,
            market_maker=observers[0],
            rows=divergent_rows,
            quorum=2,
        )

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


class FairValueObservationRepository:
    def __init__(self):
        self.rows = []
        self.marks = []

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

    def upsert_fair_value_mark(self, row):
        self.marks = [row]


class FairValueValuatorFunctions:
    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, block_identifier):
            assert block_identifier == 100
            return self.value

    def interfaceVersion(self):
        return self.Call(1)

    def valuationPolicyVersion(self):
        return self.Call(2)

    def requiredModelVersion(self):
        return self.Call(1)

    def liabilityBufferBps(self):
        return self.Call(0)

    def maxObservationDivergenceBps(self):
        return self.Call(500)

    def observationDigest(
        self,
        _adapter,
        position_id,
        _snapshot_block,
        _valid_until_block,
        _liability,
        _base_exit_cost,
        observation_nonce,
    ):
        return self.Call(Web3.keccak(text=f"{position_id}:{observation_nonce}"))

    def isApprovedObserver(self, _observer):
        return self.Call(True)


def fair_value_valuator():
    return SimpleNamespace(
        address=Web3.to_checksum_address(VALUATOR),
        functions=FairValueValuatorFunctions(),
    )


def fair_value_gateway(keys, strategy_kind="csp"):
    market_maker = Account.from_key("0x" + f"{3:064x}").address.lower()
    repository = FairValueObservationRepository()
    fund = TrustedFund({"chain_id": 84532, "fund_address": FUND}, {}, {}, None)
    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.fund = fund
    gateway.repository = repository
    gateway.strategy_kind = strategy_kind
    gateway.adapter_abi = []
    gateway.sepolia_observer_private_keys = keys
    gateway.fair_value_policy = (
        CoveredCallFairValuePolicy(
            implied_volatility_bps=4_200,
            implied_volatility_source=(
                "deribit-eth-atm-snapshot-2026-07-26T18:30:49Z-"
                "b1n358-covered-call-v2-approved"
            ),
            risk_free_rate_bps=500,
            settlement_cost_bps=0,
        )
        if strategy_kind == "covered_call"
        else FairValuePolicy(
            implied_volatility_bps=4_200,
            implied_volatility_source="approved-testnet-snapshot",
            risk_free_rate_bps=500,
            settlement_cost_bps=0,
        )
    )
    gateway.chain_id = lambda: 84532
    gateway.block_hash = lambda _block: bytes.fromhex("12" * 32)
    gateway.head_block = lambda: 101
    gateway.max_observation_window = lambda _valuator, _block: (
        120 if strategy_kind == "covered_call" else 20
    )
    gateway.observer_approved = lambda _valuator, _observer, _block: True
    gateway.market_maker = lambda _adapter, _position, _block: market_maker
    gateway.observation_digest = lambda observation: Web3.keccak(
        text=f"{observation.position_id}:{observation.observation_nonce}"
    )
    gateway._approved_spot_snapshot = lambda **_kwargs: {
        "round_id": 7,
        "price_8": 191_213_078_641,
        "updated_at": 1_785_090_000,
    }

    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, block_identifier):
            assert block_identifier == 100
            return self.value

    class AdapterFunctions:
        def accountingAsset(self):
            return Call(WETH if strategy_kind == "covered_call" else USDC)

        def weth(self):
            return Call(WETH)

        def usdc(self):
            return Call(USDC)

    class TokenFunctions:
        def decimals(self):
            return Call(18 if strategy_kind == "covered_call" else 6)

    class OTokenFunctions:
        def isPut(self):
            return Call(strategy_kind == "csp")

        def underlying(self):
            return Call(WETH)

        def strikeAsset(self):
            return Call(USDC)

        def collateralAsset(self):
            return Call(WETH if strategy_kind == "covered_call" else USDC)

        def strikePrice(self):
            return Call(
                220_000_000_000 if strategy_kind == "covered_call" else 157_500_000_000
            )

        def expiry(self):
            return Call(1_785_139_200)

    class Eth:
        def get_block(self, _block):
            return {"timestamp": 1_785_090_604}

        def contract(self, address, abi):
            functions = {
                Web3.to_checksum_address(ADAPTER): AdapterFunctions(),
                Web3.to_checksum_address(USDC): TokenFunctions(),
                Web3.to_checksum_address(WETH): TokenFunctions(),
                Web3.to_checksum_address(OTOKEN): OTokenFunctions(),
            }[Web3.to_checksum_address(address)]
            return SimpleNamespace(functions=functions)

    gateway.w3 = SimpleNamespace(eth=Eth())
    position = (
        (
            OTOKEN,
            market_maker,
            1,
            250_000,
            2_500_000_000_000_000,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
            bytes(32),
        )
        if strategy_kind == "covered_call"
        else (
            OTOKEN,
            market_maker,
            1,
            50_793_650,
            799_999_988,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
            bytes(32),
        )
    )
    return gateway, repository, position


def test_sepolia_fair_value_observations_are_exact_and_idempotent() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, repository, position = fair_value_gateway(keys)
    valuator = fair_value_valuator()

    gateway._publish_sepolia_fair_value_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        quorum=2,
        positions=[(1, position)],
        existing_rows=[],
    )
    gateway._publish_sepolia_fair_value_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        quorum=2,
        positions=[(1, position)],
        existing_rows=repository.rows,
    )

    assert len(repository.rows) == 2
    assert {int(row["liability"]) for row in repository.rows} == {1}
    assert {int(row["base_exit_cost"]) for row in repository.rows} == {0}
    assert len({row["observation_nonce"] for row in repository.rows}) == 2
    assert {
        observation_model_version(int(row["observation_nonce"]))
        for row in repository.rows
    } == {1}
    assert {row["snapshot_block_hash"] for row in repository.rows} == {"0x" + "12" * 32}
    assert repository.marks[0]["fair_liability_assets"] == "1"
    assert repository.marks[0]["stress_liability_assets"] == "799999988"
    assert repository.marks[0]["source_quality"] == "single_model_multi_signer"


def test_covered_call_publisher_restores_active_nav_observation_quorum() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, repository, position = fair_value_gateway(
        keys, strategy_kind="covered_call"
    )
    valuator = fair_value_valuator()

    gateway._publish_sepolia_fair_value_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        quorum=2,
        positions=[(1, position)],
        existing_rows=[],
    )
    accepted = gateway._position_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        position_id=1,
        market_maker=position[1],
        rows=repository.rows,
        quorum=2,
    )

    assert len(accepted) == 2
    assert len(encode_valuation_data(accepted)) > 32
    assert {row["strategy_kind"] for row in repository.rows} == {"covered_call"}
    liability = int(repository.rows[0]["liability"])
    assert 0 < liability < position[4]
    assert {int(row["base_exit_cost"]) for row in repository.rows} == {0}
    assert repository.marks[0]["strategy_kind"] == "covered_call"
    assert repository.marks[0]["model_name"] == "b1nary-european-bs-call-v1"
    assert (
        repository.marks[0]["policy_reference"]
        == "policies/covered_call_fund_policy.v2.base-sepolia.json"
    )
    assert (
        repository.marks[0]["policy_sha256"]
        == "4ecb60fc6a19ac0a10c37ca380998b3566a3193693a10fb211f86bb61a2bebf3"
    )
    assert repository.marks[0]["stress_liability_assets"] == str(position[4])
    assert repository.marks[0]["fair_liability_assets"] == str(liability)


def test_covered_call_publisher_requires_final_observation_window_policy() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, _repository, position = fair_value_gateway(
        keys, strategy_kind="covered_call"
    )
    gateway.max_observation_window = lambda _valuator, _block: 119

    with pytest.raises(RuntimeError, match="OBSERVATION_WINDOW_POLICY_MISMATCH"):
        gateway._publish_sepolia_fair_value_observations(
            valuator=fair_value_valuator(),
            adapter=ADAPTER,
            block=100,
            quorum=2,
            positions=[(1, position)],
            existing_rows=[],
        )


def test_covered_call_publisher_rejects_unapproved_bounded_policy() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, _repository, position = fair_value_gateway(
        keys, strategy_kind="covered_call"
    )
    gateway.fair_value_policy = CoveredCallFairValuePolicy(
        implied_volatility_bps=4_300,
        implied_volatility_source=(
            "deribit-eth-atm-snapshot-2026-07-26T18:30:49Z-"
            "b1n358-covered-call-v2-approved"
        ),
        risk_free_rate_bps=500,
        settlement_cost_bps=0,
    )

    with pytest.raises(RuntimeError, match="FAIR_VALUE_POLICY_MISMATCH"):
        gateway._publish_sepolia_fair_value_observations(
            valuator=fair_value_valuator(),
            adapter=ADAPTER,
            block=100,
            quorum=2,
            positions=[(1, position)],
            existing_rows=[],
        )


@pytest.mark.parametrize(
    ("feed", "decimals", "staleness"),
    [
        (ADAPTER, 8, 3_600),
        (runtime.COVERED_CALL_SPOT_FEED, 6, 3_600),
        (runtime.COVERED_CALL_SPOT_FEED, 8, 3_599),
    ],
)
def test_covered_call_spot_policy_is_exact(feed, decimals, staleness) -> None:
    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, block_identifier):
            assert block_identifier == 100
            return self.value

    class ValuatorFunctions:
        def spotFeed(self):
            return Call(feed)

        def spotFeedDecimals(self):
            return Call(decimals)

        def maxSpotStaleness(self):
            return Call(staleness)

    class Eth:
        def contract(self, address, abi):
            raise AssertionError("invalid policy must fail before feed RPC")

    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.strategy_kind = "covered_call"
    gateway.w3 = SimpleNamespace(eth=Eth())
    valuator = SimpleNamespace(functions=ValuatorFunctions())

    with pytest.raises(RuntimeError, match="COVERED_CALL_SPOT_POLICY_MISMATCH"):
        gateway._approved_spot_snapshot(
            valuator=valuator,
            block=100,
            snapshot_timestamp=1_000,
        )


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
def test_sepolia_fair_value_observations_fail_closed(mutation, reason) -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, _repository, position = fair_value_gateway(keys)
    mutation(gateway)

    with pytest.raises(RuntimeError, match=reason):
        gateway._publish_sepolia_fair_value_observations(
            valuator=fair_value_valuator(),
            adapter=ADAPTER,
            block=100,
            quorum=2,
            positions=[(1, position)],
            existing_rows=[],
        )


def test_sepolia_fair_value_observations_reject_policy_mismatch() -> None:
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    gateway, _repository, position = fair_value_gateway(keys)
    existing = [
        {
            "position_id": 1,
            "observer_address": Account.from_key(keys[0]).address.lower(),
            "liability": 2,
            "base_exit_cost": 0,
            "observation_nonce": str(1 << 192),
        }
    ]

    with pytest.raises(RuntimeError, match="POLICY_MISMATCH"):
        gateway._publish_sepolia_fair_value_observations(
            valuator=fair_value_valuator(),
            adapter=ADAPTER,
            block=100,
            quorum=2,
            positions=[(1, position)],
            existing_rows=existing,
        )


def test_sepolia_observer_key_config_is_disabled_and_strict(monkeypatch) -> None:
    assert settings.fund_csp_sepolia_fair_value_observations_enabled is False
    key = "0x" + f"{1:064x}"
    monkeypatch.setattr(settings, "fund_csp_sepolia_observer_private_keys", key)
    with pytest.raises(ValueError, match="exactly two"):
        get_fund_csp_sepolia_observer_private_keys()

    monkeypatch.setattr(
        settings, "fund_csp_sepolia_observer_private_keys", f"{key},{key}"
    )
    with pytest.raises(ValueError, match="duplicate observers"):
        get_fund_csp_sepolia_observer_private_keys()


def test_covered_call_observer_config_is_separate_disabled_and_strict(
    monkeypatch,
) -> None:
    assert settings.fund_covered_call_sepolia_fair_value_observations_enabled is False
    key = "0x" + f"{1:064x}"
    monkeypatch.setattr(
        settings, "fund_covered_call_sepolia_observer_private_keys", key
    )
    with pytest.raises(ValueError, match="exactly two"):
        get_fund_covered_call_sepolia_observer_private_keys()

    monkeypatch.setattr(
        settings,
        "fund_covered_call_sepolia_observer_private_keys",
        f"{key},{key}",
    )
    with pytest.raises(ValueError, match="duplicate observers"):
        get_fund_covered_call_sepolia_observer_private_keys()


def test_sepolia_fair_value_policy_has_no_implicit_iv_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "fund_csp_sepolia_fair_value_iv_bps", 0)
    monkeypatch.setattr(settings, "fund_csp_sepolia_fair_value_iv_source", "")

    with pytest.raises(ValueError, match="IV_BPS"):
        get_fund_csp_sepolia_fair_value_policy()


def test_covered_call_fair_value_policy_has_no_implicit_iv_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "fund_covered_call_sepolia_fair_value_iv_bps", 0)
    monkeypatch.setattr(settings, "fund_covered_call_sepolia_fair_value_iv_source", "")

    with pytest.raises(ValueError, match="IV_BPS"):
        get_fund_covered_call_sepolia_fair_value_policy()


def test_covered_call_config_requires_exact_approved_policy(monkeypatch) -> None:
    monkeypatch.setattr(settings, "fund_covered_call_sepolia_fair_value_iv_bps", 4_200)
    monkeypatch.setattr(
        settings,
        "fund_covered_call_sepolia_fair_value_iv_source",
        "unapproved-source",
    )
    monkeypatch.setattr(
        settings,
        "fund_covered_call_sepolia_fair_value_risk_free_rate_bps",
        500,
    )
    monkeypatch.setattr(
        settings,
        "fund_covered_call_sepolia_fair_value_settlement_cost_bps",
        0,
    )

    with pytest.raises(ValueError, match="approved B1N-358"):
        get_fund_covered_call_sepolia_fair_value_policy()
