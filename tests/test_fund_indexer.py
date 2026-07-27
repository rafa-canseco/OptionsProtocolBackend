import asyncio
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

    assert client.table_name == "v2_confirmed_chain_heads"
    assert client.row["chain_id"] == 84532
    assert client.row["block_number"] == 2_995
    assert client.row["block_hash"] == f"0x{2_995:064x}"
    assert client.conflict == "chain_id"


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
        lambda _: {"next_block": 2_995, "last_block_hash": None},
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
    assert client.row["block_number"] == 2_995
    assert projected_terminals == [
        (client.row["block_number"], client.row["block_hash"]),
        (client.row["block_number"], client.row["block_hash"]),
    ]


def test_run_offloads_blocking_index_cycle(monkeypatch) -> None:
    calls = []

    async def fake_to_thread(function, *args):
        calls.append((function, args))
        raise asyncio.CancelledError

    monkeypatch.setattr(indexer.settings, "rpc_url", "https://rpc.example")
    monkeypatch.setattr(indexer.asyncio, "to_thread", fake_to_thread)

    asyncio.run(indexer.run())

    assert len(calls) == 1
    assert calls[0][0] is indexer._index_registered_funds
    assert len(calls[0][1]) == 1


def test_reorg_rewinds_without_advancing_checkpoint(monkeypatch, registry) -> None:
    monkeypatch.setattr(
        indexer,
        "_checkpoint",
        lambda _: {"next_block": 200, "last_block_hash": "0xdead"},
    )
    rewinds = []
    monkeypatch.setattr(indexer, "_rewind", lambda _, block: rewinds.append(block))
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
        lambda _: {"next_block": 100, "last_block_hash": None},
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
        lambda _: {"next_block": 100, "last_block_hash": None},
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
        lambda _: {"next_block": 100, "last_block_hash": None},
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
        lambda _: {"next_block": next_block, "last_block_hash": None},
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
    monkeypatch.setattr(indexer, "_checkpoint", lambda _: checkpoint)
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
        indexer, "_checkpoint", lambda _: pytest.fail("checkpoint must not be read")
    )
    w3 = FakeWeb3()
    w3.eth.chain_id = 1

    with pytest.raises(ValueError, match="chain mismatch"):
        indexer.index_registry_once(w3, registry, _confirmed_head())


def test_settings_chain_mismatch_fails_before_checkpoint(monkeypatch, registry) -> None:
    monkeypatch.setattr(indexer.settings, "chain_id", 8453)
    monkeypatch.setattr(
        indexer, "_checkpoint", lambda _: pytest.fail("checkpoint must not be read")
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
        lambda _: {"next_block": 100, "last_block_hash": None},
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
        lambda _: {"next_block": 100, "last_block_hash": None},
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
    monkeypatch.setattr(indexer, "_load_events", lambda _: [])
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

    monkeypatch.setattr(indexer, "_load_events", lambda _: [event])
    monkeypatch.setattr(indexer, "project_events", capture_projection)
    monkeypatch.setattr(indexer, "get_client", lambda: CapturingRpcClient())
    block_hash = f"0x{115:064x}"

    indexer._persist_window(FakeWeb3(), registry, 101, 115, block_hash, [])

    assert projected_blocks == [115]


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
    monkeypatch.setattr(indexer, "_load_events", lambda _: [])
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
    monkeypatch.setattr(indexer, "_load_events", lambda _: [])
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
    monkeypatch.setattr(indexer, "_load_events", lambda _: [])
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
    query = FakeEventQuery(rows, server_limit=500)
    monkeypatch.setattr(indexer, "get_client", lambda: query)

    events = indexer._load_events(registry)

    assert len(events) == indexer.EVENT_PAGE_SIZE + 1
    assert query.ranges == [
        (0, 999),
        (500, 1_499),
        (1_000, 1_999),
        (1_001, 2_000),
    ]
    assert (
        query.orders
        == [
            "block_number",
            "transaction_index",
            "log_index",
        ]
        * 4
    )


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


class FakeEventQuery:
    def __init__(self, rows: list[dict], server_limit: int):
        self.rows = rows
        self.server_limit = server_limit
        self.orders = []
        self.ranges = []
        self.current_range = (0, len(rows))

    def table(self, _name):
        return self

    def select(self, _columns):
        return self

    def eq(self, _column, _value):
        return self

    def order(self, column):
        self.orders.append(column)
        return self

    def range(self, start, end):
        self.current_range = (start, end)
        self.ranges.append(self.current_range)
        return self

    def execute(self):
        start, end = self.current_range
        end = min(end, start + self.server_limit - 1)
        return type("Result", (), {"data": self.rows[start : end + 1]})()


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

    def table(self, name):
        self.table_name = name
        return self

    def upsert(self, row, on_conflict):
        self.row = row
        self.conflict = on_conflict
        return self

    def execute(self):
        if self.after_execute is not None:
            self.after_execute()
        return None
