import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from web3 import Web3

from src.fund_indexer import indexer
from src.fund_indexer.abis import EVENTS_BY_TOPIC
from src.fund_indexer.indexer import ContractBinding, FundRegistry


FUND = "0xf000000000000000000000000000000000000001"
USDC = "0xf000000000000000000000000000000000000002"
WETH = "0xf000000000000000000000000000000000000003"
IMPLEMENTATION = "0xf000000000000000000000000000000000000004"


class FakeEth:
    block_number = 3_000
    chain_id = 84532

    def __init__(
        self,
        hashes: dict[int, str] | None = None,
        implementations: dict[int, str] | None = None,
        missing_code: set[tuple[str, int]] | None = None,
    ):
        self.hashes = hashes or {}
        self.implementations = implementations or {}
        self.missing_code = missing_code or set()
        self.storage_blocks = []
        self.code_reads = []

    def get_block(self, block: int):
        return {"hash": bytes.fromhex(self.hashes.get(block, f"{block:064x}"))}

    def get_storage_at(self, _address, _slot, block_identifier=None):
        assert block_identifier is not None
        self.storage_blocks.append(block_identifier)
        implementation = self.implementations.get(block_identifier, IMPLEMENTATION)
        return bytes.fromhex("00" * 12 + implementation[2:])

    def get_code(self, address, block_identifier=None):
        assert block_identifier is not None
        normalized = address.lower()
        self.code_reads.append((normalized, block_identifier))
        return b"" if (normalized, block_identifier) in self.missing_code else b"\x01"


class AdvancingEth(FakeEth):
    def __init__(self):
        super().__init__()
        self.current_block = 3_000
        self.block_number_reads = 0

    @property
    def block_number(self):
        self.block_number_reads += 1
        return self.current_block

    def mine_block(self):
        self.current_block += 1


class FakeWeb3:
    def __init__(
        self,
        hashes: dict[int, str] | None = None,
        implementations: dict[int, str] | None = None,
        missing_code: set[tuple[str, int]] | None = None,
    ):
        self.eth = FakeEth(hashes, implementations, missing_code)
        self.codec = Web3().codec


def _confirmed_head(block_number: int = 2_995) -> indexer.ConfirmedHead:
    return indexer.ConfirmedHead(
        chain_id=84532,
        block_number=block_number,
        block_hash=f"0x{block_number:064x}",
    )


@pytest.fixture
def registry(monkeypatch) -> FundRegistry:
    monkeypatch.setattr(indexer.settings, "chain_id", 84532)
    return FundRegistry(
        chain_id=84532,
        fund_address=FUND,
        start_block=100,
        accounting_asset=USDC,
        weth=WETH,
        contracts=(
            ContractBinding(
                address=FUND,
                role="fund_vault",
                interface_version=1,
                valid_from_block=100,
                valid_to_block=None,
                implementation_address=IMPLEMENTATION,
            ),
        ),
    )


def test_event_topics_use_rpc_hex_format() -> None:
    assert EVENTS_BY_TOPIC
    assert all(topic.startswith("0x") for topic in EVENTS_BY_TOPIC)


def test_confirmed_head_checkpoint_is_persisted_without_api_rpc(monkeypatch) -> None:
    client = HeadClient()
    monkeypatch.setattr(indexer, "get_client", lambda: client)

    confirmed_head = indexer._capture_confirmed_head(FakeWeb3(), 84532)
    indexer._store_confirmed_head(confirmed_head)

    assert client.rpc_name == "v2_upsert_confirmed_chain_head"
    assert client.params["p_chain_id"] == 84532
    assert client.params["p_block_number"] == 2_995
    assert client.params["p_block_hash"] == f"0x{2_995:064x}"


def test_confirmed_head_advance_keeps_maximum_across_workers(monkeypatch) -> None:
    class MonotonicHeadClient:
        def __init__(self):
            self.block_number = -1
            self.block_hash = None

        def rpc(self, name, params):
            assert name == "v2_upsert_confirmed_chain_head"
            if params["p_block_number"] >= self.block_number:
                self.block_number = params["p_block_number"]
                self.block_hash = params["p_block_hash"]
            return self

        def execute(self):
            return None

    client = MonotonicHeadClient()
    monkeypatch.setattr(indexer, "get_client", lambda: client)

    for block_number in (100, 101, 99):
        indexer._store_confirmed_head(
            indexer.ConfirmedHead(
                chain_id=84532,
                block_number=block_number,
                block_hash=f"0x{block_number:064x}",
            )
        )

    assert client.block_number == 101
    assert client.block_hash == f"0x{101:064x}"


def test_confirmed_head_rejects_wrong_rpc_chain_before_persistence(monkeypatch) -> None:
    w3 = FakeWeb3()
    w3.eth.chain_id = 1
    monkeypatch.setattr(
        indexer, "get_client", lambda: pytest.fail("head must not be persisted")
    )

    with pytest.raises(ValueError, match="RPC chain mismatch"):
        indexer._capture_confirmed_head(w3, 84532)


def test_cycle_reuses_one_confirmed_head_when_rpc_advances(
    monkeypatch, registry
) -> None:
    w3 = FakeWeb3()
    w3.eth = AdvancingEth()
    client = HeadClient(after_execute=w3.eth.mine_block)
    monkeypatch.setattr(indexer, "get_client", lambda: client)
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": 2_995, "last_block_hash": None},
    )
    monkeypatch.setattr(indexer, "_fetch_window", lambda *_: [])
    projected_terminals = []
    monkeypatch.setattr(
        indexer,
        "_persist_window",
        lambda *_args: projected_terminals.append((_args[3], _args[4])),
    )

    indexer._index_cycle(w3, [registry, registry])

    assert w3.eth.block_number_reads == 1
    assert w3.eth.current_block == 3_001
    assert client.params["p_block_number"] == 2_995
    assert projected_terminals == [
        (client.params["p_block_number"], client.params["p_block_hash"]),
        (client.params["p_block_number"], client.params["p_block_hash"]),
    ]


class ClosableWorkerClient:
    def __init__(self):
        self.postgrest = self
        self.closed = False

    def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_slow_fund_does_not_delay_peer_and_worker_clients_close(
    monkeypatch, registry
) -> None:
    covered_call = replace(
        registry,
        fund_address="0xf000000000000000000000000000000000000099",
        strategy_kind="covered_call",
    )
    slow_started = threading.Event()
    release_slow = threading.Event()
    covered_call_ran = asyncio.Event()
    worker_clients = []
    cycle_clients = {}
    loop = asyncio.get_running_loop()

    def create_client():
        client = ClosableWorkerClient()
        worker_clients.append(client)
        return client

    def index_cycle(_w3, client, selected_registry):
        fund_address = selected_registry.fund_address
        cycle_clients[fund_address] = client
        if fund_address == registry.fund_address:
            slow_started.set()
            release_slow.wait(timeout=2)
            return
        loop.call_soon_threadsafe(covered_call_ran.set)

    monkeypatch.setattr(
        indexer,
        "get_tokenized_fund_rpc_url",
        lambda: "https://fund-rpc.example",
    )
    monkeypatch.setattr(
        indexer,
        "create_validated_backend_w3",
        lambda *_args: object(),
    )
    monkeypatch.setattr(indexer, "_create_worker_client", create_client)
    monkeypatch.setattr(
        indexer,
        "_load_registries",
        lambda _client=None: [registry, covered_call],
    )
    monkeypatch.setattr(
        indexer,
        "_index_registry_once",
        index_cycle,
    )

    task = asyncio.create_task(indexer.run())
    await asyncio.wait_for(covered_call_ran.wait(), timeout=1)
    assert slow_started.is_set()
    assert len(worker_clients) == 3

    task.cancel()
    await asyncio.sleep(0.05)

    # Cancellation stays responsive while executor shutdown safely waits for
    # the in-flight blocking cycle. Its client must remain open until release.
    assert not task.done()
    assert worker_clients[0].closed
    assert not all(client.closed for client in worker_clients[1:])

    release_slow.set()
    await task

    assert all(client.closed for client in worker_clients)
    assert (
        cycle_clients[registry.fund_address]
        is not cycle_clients[covered_call.fund_address]
    )


@pytest.mark.asyncio
async def test_fund_worker_error_does_not_stop_peer(monkeypatch, registry) -> None:
    covered_call = replace(
        registry,
        fund_address="0xf000000000000000000000000000000000000099",
        strategy_kind="covered_call",
    )
    failed_fund_ran = threading.Event()
    covered_call_ran = asyncio.Event()
    loop = asyncio.get_running_loop()

    def index_cycle(_w3, _client, selected_registry):
        if selected_registry.fund_address == registry.fund_address:
            failed_fund_ran.set()
            raise RuntimeError("temporary CSP indexing failure")
        loop.call_soon_threadsafe(covered_call_ran.set)

    monkeypatch.setattr(
        indexer,
        "get_tokenized_fund_rpc_url",
        lambda: "https://fund-rpc.example",
    )
    monkeypatch.setattr(
        indexer,
        "create_validated_backend_w3",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        indexer,
        "_create_worker_client",
        ClosableWorkerClient,
    )
    monkeypatch.setattr(
        indexer,
        "_load_registries",
        lambda _client=None: [registry, covered_call],
    )
    monkeypatch.setattr(
        indexer,
        "_index_registry_once",
        index_cycle,
    )

    task = asyncio.create_task(indexer.run())
    await asyncio.wait_for(covered_call_ran.wait(), timeout=1)
    task.cancel()
    await task

    assert failed_fund_ran.is_set()


@pytest.mark.asyncio
async def test_workers_construct_provider_from_selected_fund_rpc(
    monkeypatch, registry
) -> None:
    provider_urls = []
    cycle_ran = asyncio.Event()
    loop = asyncio.get_running_loop()

    def provider_factory(url, chain_id, name):
        provider_urls.append((url, chain_id, name))
        return SimpleNamespace(provider=("provider", url))

    def index_cycle(w3, *_args):
        assert w3.provider == ("provider", "https://fund-rpc.example")
        loop.call_soon_threadsafe(cycle_ran.set)

    monkeypatch.setattr(
        indexer,
        "get_tokenized_fund_rpc_url",
        lambda: "https://fund-rpc.example",
    )
    monkeypatch.setattr(indexer, "create_validated_backend_w3", provider_factory)
    monkeypatch.setattr(
        indexer,
        "_create_worker_client",
        ClosableWorkerClient,
    )
    monkeypatch.setattr(
        indexer,
        "_load_registries",
        lambda _client=None: [registry],
    )
    monkeypatch.setattr(
        indexer,
        "_index_registry_once",
        index_cycle,
    )

    task = asyncio.create_task(indexer.run())
    await asyncio.wait_for(cycle_ran.wait(), timeout=1)
    task.cancel()
    await task

    assert provider_urls == [
        ("https://fund-rpc.example", registry.chain_id, "TOKENIZED_FUND_RPC_URL")
    ]


@pytest.mark.asyncio
async def test_indexer_worker_failure_backoff_caps_and_resets(
    monkeypatch, registry
) -> None:
    outcomes = iter(
        [RuntimeError("one"), RuntimeError("two"), None, RuntimeError("three")]
    )
    delays = []

    def index_once(*_args):
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "_create_worker_client", ClosableWorkerClient)
    monkeypatch.setattr(indexer, "create_validated_backend_w3", lambda *_args: object())
    monkeypatch.setattr(indexer, "_index_registry_once", index_once)
    monkeypatch.setattr(
        indexer.settings, "tokenized_fund_indexer_poll_interval_seconds", 200
    )
    monkeypatch.setattr(indexer.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await indexer._run_registry_worker(registry, "https://fund-rpc.example")

    assert delays == [200, 300, 200, 200]


@pytest.mark.asyncio
async def test_indexer_worker_refreshes_only_on_bounded_interval(
    monkeypatch, registry
) -> None:
    monotonic_values = iter([0, 100, 200, 300, 400, 500])
    refreshes = []
    cycles = []
    sleeps = 0

    def refresh(_client, chain_id, fund_address):
        refreshes.append((chain_id, fund_address))
        return registry

    async def sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "_create_worker_client", ClosableWorkerClient)
    monkeypatch.setattr(indexer, "create_validated_backend_w3", lambda *_args: object())
    monkeypatch.setattr(indexer, "_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(indexer, "_load_registry_identity", refresh)
    monkeypatch.setattr(
        indexer,
        "_index_registry_once",
        lambda _w3, _client, selected: cycles.append(selected),
    )
    monkeypatch.setattr(indexer.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await indexer._run_registry_worker(registry, "https://fund-rpc.example")

    assert len(cycles) == 4
    assert refreshes == [(registry.chain_id, registry.fund_address)]


def test_registry_identity_refresh_uses_specific_explicit_queries(registry) -> None:
    calls = []
    registry_row = {
        "chain_id": registry.chain_id,
        "fund_address": registry.fund_address,
        "start_block": registry.start_block,
        "accounting_asset": registry.accounting_asset,
        "weth": registry.weth,
        "strategy_kind": registry.strategy_kind,
        "quote_asset": registry.quote_asset,
    }
    contract = registry.contracts[0]
    contract_row = {
        "contract_address": contract.address,
        "contract_role": contract.role,
        "interface_version": contract.interface_version,
        "valid_from_block": contract.valid_from_block,
        "valid_to_block": contract.valid_to_block,
        "implementation_address": contract.implementation_address,
    }

    class Query:
        def __init__(self, table_name, data):
            self.table_name = table_name
            self.data = data

        def select(self, columns):
            calls.append((self.table_name, "select", columns))
            return self

        def eq(self, column, value):
            calls.append((self.table_name, "eq", column, value))
            return self

        def limit(self, value):
            calls.append((self.table_name, "limit", value))
            return self

        def execute(self):
            return self

    class Client:
        def table(self, name):
            data = [registry_row] if name == "v2_fund_registry" else [contract_row]
            return Query(name, data)

    loaded = indexer._load_registry_identity(
        Client(), registry.chain_id, registry.fund_address
    )

    assert loaded == registry
    assert ("v2_fund_registry", "select", indexer.FUND_REGISTRY_COLUMNS) in calls
    assert ("v2_fund_registry", "eq", "enabled", True) in calls
    assert ("v2_fund_registry", "eq", "chain_id", registry.chain_id) in calls
    assert (
        "v2_fund_registry",
        "eq",
        "fund_address",
        registry.fund_address,
    ) in calls
    assert ("v2_fund_contracts", "select", indexer.FUND_CONTRACT_COLUMNS) in calls
    assert not any(call[1] == "select" and call[2] == "*" for call in calls)


def test_reorg_rewinds_without_advancing_checkpoint(monkeypatch, registry) -> None:
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": 200, "last_block_hash": "0xdead"},
    )
    rewinds = []
    monkeypatch.setattr(
        indexer, "_rewind", lambda _, block, *_rest: rewinds.append(block)
    )
    monkeypatch.setattr(
        indexer,
        "_fetch_window",
        lambda *_: pytest.fail("must not fetch before rewinding"),
    )

    count = indexer.index_registry_once(FakeWeb3(), registry, _confirmed_head())

    assert count == 0
    assert rewinds == [100]


def test_rpc_range_error_reduces_window(monkeypatch, registry) -> None:
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": 100, "last_block_hash": None},
    )
    calls = []

    def fetch(_, __, from_block, to_block):
        calls.append((from_block, to_block))
        if len(calls) == 1:
            raise RuntimeError("provider returned too many results")
        return []

    persisted = []
    monkeypatch.setattr(indexer, "_fetch_window", fetch)
    monkeypatch.setattr(
        indexer,
        "_persist_window",
        lambda *args: persisted.append(args[2:]),
    )

    count = indexer.index_registry_once(FakeWeb3(), registry, _confirmed_head())

    assert count == 0
    assert calls == [(100, 2_099), (100, 1_099)]
    assert persisted[0][0:2] == (100, 1_099)


def test_window_stops_before_next_contract_binding(monkeypatch, registry) -> None:
    boundary_binding = ContractBinding(
        USDC, "claim_escrow", 1, 200, None, implementation_address=None
    )
    registry = FundRegistry(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        start_block=registry.start_block,
        accounting_asset=registry.accounting_asset,
        weth=registry.weth,
        contracts=(*registry.contracts, boundary_binding),
    )
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": 100, "last_block_hash": None},
    )
    fetched = []
    monkeypatch.setattr(
        indexer,
        "_fetch_window",
        lambda *args: fetched.append(args[2:]) or [],
    )
    monkeypatch.setattr(indexer, "_persist_window", lambda *_: None)
    w3 = FakeWeb3()

    indexer.index_registry_once(w3, registry, _confirmed_head())

    assert fetched == [(100, 199)]
    assert w3.eth.storage_blocks == [100, 199]
    assert w3.eth.code_reads == [
        (FUND, 100),
        (IMPLEMENTATION, 100),
        (FUND, 199),
        (IMPLEMENTATION, 199),
    ]


def test_window_stops_when_binding_expires_without_successor(
    monkeypatch, registry
) -> None:
    expiring = ContractBinding(FUND, "fund_vault", 1, 100, 199, IMPLEMENTATION)
    registry = FundRegistry(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        start_block=registry.start_block,
        accounting_asset=registry.accounting_asset,
        weth=registry.weth,
        contracts=(expiring,),
    )
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": 100, "last_block_hash": None},
    )
    fetched = []
    monkeypatch.setattr(
        indexer, "_fetch_window", lambda *args: fetched.append(args[2:]) or []
    )
    monkeypatch.setattr(indexer, "_persist_window", lambda *_: None)

    indexer.index_registry_once(FakeWeb3(), registry, _confirmed_head())

    assert fetched == [(100, 199)]


@pytest.mark.parametrize("mismatch_block", [199, 200])
def test_proxy_mismatch_at_terminal_or_new_binding_stops_ingestion(
    monkeypatch, registry, mismatch_block
) -> None:
    old_binding = ContractBinding(FUND, "fund_vault", 1, 100, 199, IMPLEMENTATION)
    new_binding = ContractBinding(FUND, "fund_vault", 1, 200, None, IMPLEMENTATION)
    registry = FundRegistry(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        start_block=registry.start_block,
        accounting_asset=registry.accounting_asset,
        weth=registry.weth,
        contracts=(old_binding, new_binding),
    )
    next_block = 100 if mismatch_block == 199 else 200
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": next_block, "last_block_hash": None},
    )
    fetched = []
    monkeypatch.setattr(
        indexer,
        "_fetch_window",
        lambda *args: fetched.append(args[2:]) or [],
    )
    monkeypatch.setattr(
        indexer, "_persist_window", lambda *_: pytest.fail("must not persist")
    )
    mismatch = "0xf000000000000000000000000000000000000099"

    with pytest.raises(ValueError, match=f"at block {mismatch_block}"):
        indexer.index_registry_once(
            FakeWeb3(implementations={mismatch_block: mismatch}),
            registry,
            _confirmed_head(),
        )

    assert fetched == ([(100, 199)] if mismatch_block == 199 else [])


def test_binding_valid_to_is_inclusive(registry) -> None:
    binding = ContractBinding(FUND, "fund_vault", 1, 100, 199, IMPLEMENTATION)
    registry = FundRegistry(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        start_block=registry.start_block,
        accounting_asset=registry.accounting_asset,
        weth=registry.weth,
        contracts=(binding,),
    )

    assert list(indexer._active_bindings(registry, 199)) == [binding]
    assert list(indexer._active_bindings(registry, 200)) == []


def test_persistence_failure_does_not_run_another_window(monkeypatch, registry) -> None:
    checkpoint = {"next_block": 100, "last_block_hash": None}
    monkeypatch.setattr(indexer, "_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(indexer, "_fetch_window", lambda *_: [])
    monkeypatch.setattr(
        indexer,
        "_persist_window",
        lambda *_: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        indexer.index_registry_once(FakeWeb3(), registry, _confirmed_head())

    assert checkpoint == {"next_block": 100, "last_block_hash": None}


def test_chain_mismatch_fails_before_checkpoint(monkeypatch, registry) -> None:
    monkeypatch.setattr(
        indexer, "_checkpoint", lambda *_: pytest.fail("checkpoint must not be read")
    )
    w3 = FakeWeb3()
    w3.eth.chain_id = 1

    with pytest.raises(ValueError, match="chain mismatch"):
        indexer.index_registry_once(w3, registry, _confirmed_head())


def test_settings_chain_mismatch_fails_before_checkpoint(monkeypatch, registry) -> None:
    monkeypatch.setattr(indexer.settings, "chain_id", 8453)
    monkeypatch.setattr(
        indexer, "_checkpoint", lambda *_: pytest.fail("checkpoint must not be read")
    )

    with pytest.raises(ValueError, match="chain mismatch"):
        indexer.index_registry_once(FakeWeb3(), registry, _confirmed_head())


def test_proxy_binding_requires_registered_implementation() -> None:
    binding = ContractBinding(FUND, "fund_vault", 1, 100, None)

    with pytest.raises(ValueError, match="requires implementation_address"):
        indexer._validate_binding(binding)


def test_unsupported_binding_version_fails_without_events() -> None:
    binding = ContractBinding(FUND, "claim_escrow", 2, 100, None)

    with pytest.raises(ValueError, match="Unsupported interface version"):
        indexer._validate_binding(binding)


def test_proxy_slot_mismatch_stops_before_log_fetch(monkeypatch, registry) -> None:
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": 100, "last_block_hash": None},
    )
    w3 = FakeWeb3()
    w3.eth.get_storage_at = lambda *_args, **_kwargs: bytes.fromhex("00" * 32)
    monkeypatch.setattr(
        indexer, "_fetch_window", lambda *_: pytest.fail("logs must not be fetched")
    )

    with pytest.raises(ValueError, match="Proxy implementation mismatch"):
        indexer.index_registry_once(w3, registry, _confirmed_head())


@pytest.mark.parametrize(
    ("address", "message"),
    [(FUND, "fund_vault"), (IMPLEMENTATION, "fund_vault implementation")],
)
def test_missing_registered_bytecode_stops_before_log_fetch(
    monkeypatch, registry, address, message
) -> None:
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda *_: {"next_block": 100, "last_block_hash": None},
    )
    monkeypatch.setattr(
        indexer, "_fetch_window", lambda *_: pytest.fail("logs must not be fetched")
    )

    with pytest.raises(ValueError, match=f"Missing bytecode for {message}"):
        indexer.index_registry_once(
            FakeWeb3(missing_code={(address.lower(), 100)}),
            registry,
            _confirmed_head(),
        )


def test_unknown_upgraded_implementation_is_rejected(registry) -> None:
    upgraded_abi = next(
        abi for abi in EVENTS_BY_TOPIC.values() if abi["name"] == "Upgraded"
    )
    topic = next(topic for topic, abi in EVENTS_BY_TOPIC.items() if abi is upgraded_abi)
    unknown = "0xf000000000000000000000000000000000000099"
    log = {
        "address": FUND,
        "blockNumber": 100,
        "blockHash": bytes.fromhex("01" * 32),
        "transactionHash": bytes.fromhex("02" * 32),
        "transactionIndex": 0,
        "logIndex": 0,
        "topics": [Web3.to_bytes(hexstr=topic), bytes.fromhex("00" * 12 + unknown[2:])],
        "data": b"",
    }

    with pytest.raises(ValueError, match="Unregistered implementation"):
        indexer._decode_log(FakeWeb3(), registry, {FUND: list(registry.contracts)}, log)


def test_terminal_hash_change_prevents_rpc_persistence(monkeypatch, registry) -> None:
    monkeypatch.setattr(indexer, "_load_events", lambda *_: [])
    monkeypatch.setattr(
        indexer, "get_client", lambda: pytest.fail("database RPC must not run")
    )
    w3 = FakeWeb3({100: "bb".zfill(64)})

    with pytest.raises(RuntimeError, match="changed before persistence"):
        indexer._persist_window(w3, registry, 100, 100, "0x" + "aa".zfill(64), [])


def test_persist_window_projects_nav_at_terminal_block(monkeypatch, registry) -> None:
    event = indexer.FundEvent(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        contract_address=registry.fund_address,
        contract_role="fund_vault",
        interface_version=1,
        block_number=100,
        block_hash=f"0x{100:064x}",
        transaction_hash="0x01",
        transaction_index=0,
        log_index=0,
        event_name="NavCommitted",
        args={
            "reportNonce": 1,
            "netAssets": 1_000,
            "validAfterBlock": 110,
            "validUntilBlock": 120,
        },
    )
    projected_blocks = []

    def capture_projection(*_args, **kwargs):
        projected_blocks.append(kwargs["to_block"])
        return None

    monkeypatch.setattr(indexer, "_load_events", lambda *_: [event])
    monkeypatch.setattr(indexer, "project_events", capture_projection)
    monkeypatch.setattr(indexer, "get_client", lambda: CapturingRpcClient())
    block_hash = f"0x{115:064x}"

    indexer._persist_window(FakeWeb3(), registry, 101, 115, block_hash, [])

    assert projected_blocks == [115]


def test_persist_window_sends_only_current_history_delta(monkeypatch, registry) -> None:
    historical = indexer.FundEvent(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        contract_address=registry.fund_address,
        contract_role="fund_accounting",
        interface_version=1,
        block_number=99,
        block_hash=f"0x{99:064x}",
        transaction_hash="0x01",
        transaction_index=0,
        log_index=0,
        event_name="NavCommitted",
        args={
            "reportNonce": 1,
            "netAssets": 1_000,
            "validAfterBlock": 99,
            "validUntilBlock": 109,
        },
    )
    current = replace(
        historical,
        block_number=100,
        block_hash=f"0x{100:064x}",
        transaction_hash="0x02",
        log_index=1,
        args={
            "reportNonce": 2,
            "netAssets": 1_001,
            "validAfterBlock": 100,
            "validUntilBlock": 110,
        },
    )
    client = CapturingRpcClient()
    monkeypatch.setattr(indexer, "_load_events", lambda *_: [historical])
    monkeypatch.setattr(
        indexer,
        "_missing_reconciliation_roles",
        lambda *_: ["covered_call_adapter"],
    )
    monkeypatch.setattr(indexer, "get_client", lambda: client)

    indexer._persist_window(
        FakeWeb3(),
        registry,
        100,
        100,
        f"0x{100:064x}",
        [current],
    )

    projection = client.params["p_projection"]
    assert [row["report_nonce"] for row in projection["nav_reports"]] == [2]
    assert [row["block_number"] for row in projection["activities"]] == [100]


def test_staggered_activation_defers_reconciliation_and_advances_state(
    monkeypatch, registry
) -> None:
    event = indexer.FundEvent(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        contract_address=registry.fund_address,
        contract_role="fund_vault",
        interface_version=1,
        block_number=100,
        block_hash=f"0x{100:064x}",
        transaction_hash="0x01",
        transaction_index=0,
        log_index=0,
        event_name="Upgraded",
        args={"implementation": IMPLEMENTATION},
    )
    client = CapturingRpcClient()
    indexed_at = "2026-07-27T22:03:44+00:00"
    block_hash = f"0x{101:064x}"
    monkeypatch.setattr(indexer, "_load_events", lambda *_: [])
    monkeypatch.setattr(
        indexer,
        "_missing_reconciliation_roles",
        lambda *_: ["covered_call_adapter"],
    )
    monkeypatch.setattr(indexer, "_block_timestamp", lambda *_: indexed_at)
    monkeypatch.setattr(
        indexer,
        "read_onchain_snapshot",
        lambda *_: pytest.fail("snapshot must wait for every reconciliation role"),
    )
    monkeypatch.setattr(indexer, "get_client", lambda: client)

    indexer._persist_window(FakeWeb3(), registry, 100, 101, block_hash, [event])

    state = client.params["p_projection"]["fund_state"][0]
    assert state["as_of_block"] == 101
    assert state["as_of_block_hash"] == block_hash
    assert state["indexed_at"] == indexed_at
    assert state["reconciled"] is False
    assert client.params["p_projection"]["reconciliations"] == []


def test_authoritative_accounting_state_is_reconciled_then_persisted(
    monkeypatch, registry
) -> None:
    reporter = "0xf000000000000000000000000000000000000010"
    replacement = "0xf000000000000000000000000000000000000011"
    event = indexer.FundEvent(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        contract_address=registry.fund_address,
        contract_role="fund_accounting",
        interface_version=1,
        block_number=100,
        block_hash=f"0x{100:064x}",
        transaction_hash="0x01",
        transaction_index=0,
        log_index=0,
        event_name="ReporterSetUpdated",
        args={"version": 1, "threshold": 1, "reporters": [reporter]},
    )
    snapshot = SimpleNamespace(
        strategy_positions_hash="0x02",
        reporter_set_version=2,
        reporter_threshold=1,
        active_reporter_count=1,
        active_reporters=(replacement,),
        fee_recipient=reporter,
        management_fee_wad=0,
        performance_fee_bps=0,
        high_water_mark=1,
        last_report_nonce=7,
        accounted_idle_assets=12,
        virtual_shares=10**18,
        deposits_paused=False,
        redemptions_paused=True,
        execution_lock_owner="0x0000000000000000000000000000000000000000",
        has_active_processing=False,
        fund_flow_nonce=4,
        idle_state_hash="0x03",
        block_number=100,
        block_hash=f"0x{100:064x}",
    )
    observed_projected_state = []

    def reconcile_before_overwrite(projection, _snapshot):
        observed_projected_state.append(
            (projection.fund["active_reporters"], projection.fund["last_report_nonce"])
        )
        return {"passed": False}

    client = CapturingRpcClient()
    monkeypatch.setattr(indexer, "_load_events", lambda *_: [])
    monkeypatch.setattr(indexer, "_missing_reconciliation_roles", lambda *_: [])
    monkeypatch.setattr(indexer, "_snapshot_contracts", lambda *_: object())
    monkeypatch.setattr(indexer, "read_onchain_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(indexer, "reconcile", reconcile_before_overwrite)
    monkeypatch.setattr(indexer, "get_client", lambda: client)
    block_hash = f"0x{100:064x}"

    indexer._persist_window(FakeWeb3(), registry, 100, 100, block_hash, [event])

    state = client.params["p_projection"]["fund_state"][0]
    assert observed_projected_state == [([reporter], 0)]
    assert state["active_reporters"] == [replacement]
    assert state["last_report_nonce"] == 7
    assert state["accounted_idle_assets"] == "12"
    assert state["as_of_block"] == 100
    assert state["reconciled"] is False


def test_ambiguous_rpc_response_retries_exact_window(monkeypatch, registry) -> None:
    monkeypatch.setattr(indexer, "_load_events", lambda *_: [])
    client = AmbiguousRpcClient()
    monkeypatch.setattr(indexer, "get_client", lambda: client)
    block_hash = "0x" + f"{100:064x}"

    with pytest.raises(TimeoutError, match="response lost"):
        indexer._persist_window(FakeWeb3(), registry, 100, 100, block_hash, [])
    indexer._persist_window(FakeWeb3(), registry, 100, 100, block_hash, [])

    assert client.calls[0] == client.calls[1]
    assert client.calls[1]["p_from_block"] == 100
    assert client.calls[1]["p_to_block"] == 100
    assert client.calls[1]["p_last_block_hash"] == block_hash


def test_load_events_paginates_in_canonical_order(monkeypatch, registry) -> None:
    rows = [_event_row(index) for index in range(indexer.EVENT_PAGE_SIZE + 1)]
    client = FakeEventClient(rows, server_limit=500)
    monkeypatch.setattr(indexer, "get_client", lambda: client)

    events = indexer._load_events(registry)

    assert len(events) == indexer.EVENT_PAGE_SIZE + 1
    assert client.ranges == [
        (0, 999),
        (500, 1_499),
        (1_000, 1_999),
        (1_001, 2_000),
    ]
    assert client.orders[:3] == [
        "block_number",
        "transaction_index",
        "log_index",
    ]


def test_load_events_compacts_high_frequency_nav_history(monkeypatch, registry) -> None:
    rows = []
    for nonce in range(1, 1_501):
        submitted = _event_row(nonce * 2)
        submitted.update(
            event_name="NavSubmitted",
            payload={"reportNonce": nonce},
        )
        committed = _event_row(nonce * 2 + 1)
        committed.update(
            event_name="NavCommitted",
            payload={"reportNonce": nonce},
        )
        rows.extend((submitted, committed))
    deposit = _event_row(4_000)
    rows.append(deposit)
    client = FakeEventClient(rows, server_limit=500)
    monkeypatch.setattr(indexer, "get_client", lambda: client)

    events = indexer._load_events(registry)

    assert [event.event_name for event in events] == [
        "NavSubmitted",
        "NavCommitted",
        "Deposit",
    ]
    assert [event.args.get("reportNonce") for event in events[:2]] == [1_500, 1_500]


def _event_row(index: int) -> dict:
    return {
        "chain_id": 84532,
        "fund_address": FUND,
        "contract_address": FUND,
        "contract_role": "fund_vault",
        "interface_version": 1,
        "block_number": 100 + index // 2,
        "block_hash": f"0x{index:064x}",
        "transaction_hash": f"0x{index + 1:064x}",
        "transaction_index": index % 2,
        "log_index": index,
        "event_name": "Deposit",
        "payload": {},
    }


class FakeEventClient:
    def __init__(self, rows: list[dict], server_limit: int):
        self.rows = rows
        self.server_limit = server_limit
        self.orders = []
        self.ranges = []

    def table(self, _name):
        return FakeEventQuery(self)


class FakeEventQuery:
    def __init__(self, client: FakeEventClient):
        self.client = client
        self.rows = list(client.rows)
        self.current_range = None
        self.current_limit = None
        self.ordering = []
        self.negated = False

    def select(self, _columns):
        return self

    def eq(self, column, value):
        if column == "event_name":
            self.rows = [row for row in self.rows if row[column] == value]
        return self

    @property
    def not_(self):
        self.negated = True
        return self

    def in_(self, column, values):
        selected = set(values)
        if self.negated:
            self.rows = [row for row in self.rows if row[column] not in selected]
            self.negated = False
        else:
            self.rows = [row for row in self.rows if row[column] in selected]
        return self

    def order(self, column, *, desc=False):
        self.client.orders.append(column)
        self.ordering.append((column, desc))
        return self

    def range(self, start, end):
        self.current_range = (start, end)
        self.client.ranges.append(self.current_range)
        return self

    def limit(self, count):
        self.current_limit = count
        return self

    def execute(self):
        rows = list(self.rows)
        for column, descending in reversed(self.ordering):
            rows.sort(key=lambda row: row[column], reverse=descending)
        if self.current_range is not None:
            start, end = self.current_range
            end = min(end, start + self.client.server_limit - 1)
            rows = rows[start : end + 1]
        if self.current_limit is not None:
            rows = rows[: self.current_limit]
        return type("Result", (), {"data": rows})()


class AmbiguousRpcClient:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        assert name == "v2_ingest_fund_window"
        self.calls.append(params)
        return self

    def execute(self):
        if len(self.calls) == 1:
            raise TimeoutError("response lost")
        return None


class CapturingRpcClient:
    def rpc(self, name, params):
        assert name == "v2_ingest_fund_window"
        self.params = params
        return self

    def execute(self):
        return None


class HeadClient:
    def __init__(self, after_execute=None):
        self.after_execute = after_execute

    def rpc(self, name, params):
        self.rpc_name = name
        self.params = params
        return self

    def execute(self):
        if self.after_execute is not None:
            self.after_execute()
        return None
