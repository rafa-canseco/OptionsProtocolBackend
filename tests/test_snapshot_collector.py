import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import pytest
from web3 import Web3

import src.fund_indexer.collector as collector_module
from src.fund_indexer.collector import (
    MAX_ACTIVE_FUNDS,
    MAX_EVENT_BLOCK_RANGE,
    MAX_RETRIES_PER_RPC,
    MAX_RETRY_CALLS_PER_WINDOW,
    MAX_SNAPSHOT_AGE_FOR_MM_SECONDS,
    MIN_WINDOW_REMAINING_TO_CLAIM_SECONDS,
    REFRESH_INTERVAL_SECONDS,
    ActiveFund,
    BlockHeader,
    Claim,
    DeribitMarketDataReader,
    FundRead,
    MarketObservation,
    SupabaseCoordinator,
    SnapshotCollector,
    Web3SnapshotRPC,
    checkpoint_hash_mode,
    event_range,
    next_window_delay,
    snapshot_collector_startup_healthy,
)
from src.fund_indexer.indexer import ContractBinding, FundRegistry
from src.fund_indexer.projector import FundProjection
from src.main import snapshot_collector_health as snapshot_collector_health_endpoint
from src.pricing.deribit import IVResult

NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


class FakeCoordinator:
    def __init__(self, funds, *, available=True, retry_budget=4):
        self.fund_rows = funds
        self.available = available
        self.retry_budget = retry_budget
        self.lock = Lock()
        self.claimed = False
        self.retries = 0
        self.published = []
        self.failed = []

    def claim(self, _environment, _chain_id):
        with self.lock:
            if not self.available or self.claimed:
                return None
            self.claimed = True
            return Claim("token", 10, NOW + timedelta(seconds=20))

    def funds(self, _chain_id):
        return self.fund_rows

    def event_start(self, _funds):
        return 101

    def build_ingestions(self, _funds, _logs, _header):
        return []

    def schedule_backfill(self, *_args):
        self.backfill = _args

    def reserve_retry(self, *_args):
        with self.lock:
            if self.retries >= self.retry_budget:
                return False
            self.retries += 1
            return True

    def publish(
        self, _claim, _environment, _chain_id, header, common, funds, ingestions
    ):
        self.published.append((header, common, funds, ingestions))
        return 7

    def fail(self, *_args):
        self.failed.append(_args[-1])


class FakeRPC:
    def __init__(
        self,
        state=None,
        header_failures=0,
        fund_failures=0,
        chain_id=84532,
        startup_error=None,
    ):
        self.state = state or complete_state()
        self.chain_id = chain_id
        self.startup_error = startup_error
        self.validate_calls = 0
        self.header_failures = header_failures
        self.fund_failures = fund_failures
        self.header_calls = 0
        self.fund_calls = 0

    def validate_chain(self, chain_id):
        self.validate_calls += 1
        if self.startup_error or self.chain_id != chain_id:
            raise self.startup_error or RuntimeError("provider details must be hidden")

    def safe_header(self):
        self.header_calls += 1
        if self.header_calls <= self.header_failures:
            raise RuntimeError("Safe block RPC failed")
        return BlockHeader(100, "0x" + "ab" * 32, NOW)

    def event_logs(self, _funds, _from_block, _to_block):
        return []

    def fund_state(self, _fund, _header):
        self.fund_calls += 1
        if self.fund_calls <= self.fund_failures:
            raise RuntimeError("Snapshot Multicall RPC failed")
        return FundRead(self.state, reconciled=True, common=complete_common())


class FakeMarketReader:
    def read(self, _deadline):
        return MarketObservation(
            iv=0.6, iv_source="deribit", observed_at=int(NOW.timestamp())
        )


def fund(kind="csp"):
    return ActiveFund("base-sepolia:csp", kind, "0x" + "01" * 20, None, None)


def fixture():
    return json.loads(Path("tests/fixtures/rpc_snapshot_envelope.json").read_text())


def complete_state():
    return fixture()["funds"][0]["state"]


def complete_common():
    return fixture()["common"]


def collector(coordinator, rpc, **kwargs):
    return SnapshotCollector(
        coordinator,
        rpc,
        "staging",
        84532,
        market_reader=FakeMarketReader(),
        now=lambda: NOW,
        sleep=lambda _delay: None,
        jitter=lambda: 0,
        **kwargs,
    )


def test_constants_match_global_budget() -> None:
    assert (
        REFRESH_INTERVAL_SECONDS,
        MAX_ACTIVE_FUNDS,
        MAX_RETRIES_PER_RPC,
        MAX_RETRY_CALLS_PER_WINDOW,
        MAX_SNAPSHOT_AGE_FOR_MM_SECONDS,
        MIN_WINDOW_REMAINING_TO_CLAIM_SECONDS,
        MAX_EVENT_BLOCK_RANGE,
    ) == (30, 3, 1, 4, 45, 10, 2_000)


def test_web3_startup_chain_validation_is_counted_once_outside_recurrent_budget() -> (
    None
):
    rpc = Web3SnapshotRPC("http://unused")
    calls = []

    class Provider:
        _request_kwargs = {}

        @staticmethod
        def make_request(method, params):
            calls.append((method, params))
            return {"result": hex(84532)}

    rpc.w3 = type("W3", (), {"provider": Provider()})()

    rpc.validate_chain(84532)
    rpc.validate_chain(84532)

    assert calls == [("eth_chainId", [])]
    assert rpc.request_counts == {
        "startup_rpc": 1,
        "recurrent_rpc": 0,
        "backfill_rpc": 0,
    }


@pytest.mark.parametrize(
    "rpc",
    [FakeRPC(chain_id=8453), FakeRPC(startup_error=OSError("secret provider URL"))],
)
def test_startup_chain_validation_fails_before_claim_or_publication(rpc) -> None:
    coordinator = FakeCoordinator([fund()])
    instance = collector(coordinator, rpc)

    assert instance.collect_once() is None
    assert rpc.validate_calls == 1
    assert coordinator.claimed is False
    assert coordinator.published == []
    assert coordinator.failed == []
    assert rpc.header_calls == rpc.fund_calls == 0
    assert snapshot_collector_startup_healthy("staging", 84532) is False


def test_snapshot_collector_health_fails_closed_before_chain_validation(
    monkeypatch,
) -> None:
    rpc = FakeRPC(chain_id=8453)
    collector(FakeCoordinator([fund()]), rpc).collect_once()
    monkeypatch.setattr(
        collector_module.settings, "rpc_snapshot_collector_enabled", True
    )
    monkeypatch.setattr(collector_module.settings, "app_env", "staging")
    monkeypatch.setattr(collector_module.settings, "chain_id", 84532)

    response = asyncio.run(snapshot_collector_health_endpoint())

    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "unhealthy"}


def test_successful_startup_chain_validation_is_cached_for_collector_lifetime() -> None:
    coordinator = FakeCoordinator([fund()])
    rpc = FakeRPC()
    instance = collector(coordinator, rpc)

    assert instance.collect_once() == 7
    assert instance.collect_once() is None
    assert rpc.validate_calls == 1
    assert snapshot_collector_startup_healthy("staging", 84532) is True


def test_concurrent_collectors_issue_rpc_only_for_claim_winner() -> None:
    coordinator = FakeCoordinator([fund()])
    first_rpc, second_rpc = FakeRPC(), FakeRPC()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: collector(coordinator, item).collect_once(),
                (first_rpc, second_rpc),
            )
        )
    assert sorted(result for result in results if result is not None) == [7]
    assert first_rpc.header_calls + second_rpc.header_calls == 1
    assert first_rpc.fund_calls + second_rpc.fund_calls == 1


def test_postgres_unavailable_has_no_rpc_fallback() -> None:
    class Unavailable(FakeCoordinator):
        def claim(self, *_args):
            raise RuntimeError("database unavailable")

    rpc = FakeRPC()
    assert collector(Unavailable([fund()]), rpc).collect_once() is None
    assert rpc.header_calls == rpc.fund_calls == 0


def test_one_retry_is_reserved_atomically_before_rpc() -> None:
    coordinator = FakeCoordinator([fund()])
    rpc = FakeRPC(header_failures=1)
    assert collector(coordinator, rpc).collect_once() == 7
    assert rpc.header_calls == 2
    assert coordinator.retries == 1


def test_deadline_prevents_retry() -> None:
    coordinator = FakeCoordinator([fund()])
    coordinator.claim = lambda *_args: Claim("token", 10, NOW)
    rpc = FakeRPC(header_failures=2)
    assert collector(coordinator, rpc).collect_once() is None
    assert rpc.header_calls == 0
    assert coordinator.retries == 0


def test_missing_required_section_rejects_whole_snapshot() -> None:
    coordinator = FakeCoordinator([fund()])
    rpc = FakeRPC(state={"allocator": {}, "operations": {}})
    assert collector(coordinator, rpc).collect_once() is None
    assert coordinator.published == []
    assert coordinator.failed == ["RuntimeError"]


def test_more_than_three_funds_fails_before_rpc() -> None:
    coordinator = FakeCoordinator([fund() for _ in range(4)])
    rpc = FakeRPC()
    assert collector(coordinator, rpc).collect_once() is None
    assert rpc.header_calls == 0


def test_two_consecutive_market_reads_use_loop_local_clients(monkeypatch) -> None:
    calls = []

    async def fake_read(timeout):
        calls.append(timeout)
        return IVResult(0.6, "deribit")

    reader = DeribitMarketDataReader()
    monkeypatch.setattr(reader, "_read_iv", fake_read)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=1)

    assert reader.read(deadline).iv == 0.6
    assert reader.read(deadline).iv == 0.6
    assert len(calls) == 2


def test_offchain_market_timeout_rejects_window(monkeypatch) -> None:
    async def blocked(_timeout):
        await asyncio.sleep(1)
        return IVResult(0.6, "deribit")

    reader = DeribitMarketDataReader()
    monkeypatch.setattr(reader, "_read_iv", blocked)
    with pytest.raises(TimeoutError):
        reader.read(datetime.now(timezone.utc) + timedelta(milliseconds=1))


def test_checkpoint_hash_verification_respects_evm_lookback() -> None:
    assert checkpoint_hash_mode(1000, 1000) == "header"
    assert checkpoint_hash_mode(744, 1000) == "blockhash"
    assert checkpoint_hash_mode(743, 1000) == "backfill"
    assert checkpoint_hash_mode(1001, 1000) == "future"


def test_collector_starting_in_final_ten_seconds_realigns_next_window() -> None:
    assert next_window_delay(25.0) == pytest.approx(5.05)
    assert next_window_delay(29.9) == pytest.approx(0.15)


def test_real_event_log_is_decoded_into_atomic_ingestion(monkeypatch) -> None:
    address = "0x" + "44" * 20
    registry = FundRegistry(
        chain_id=84532,
        fund_address=address,
        start_block=1,
        accounting_asset="0x" + "22" * 20,
        weth="0x" + "55" * 20,
        contracts=(ContractBinding(address, "fund_vault", 1, 1, 100),),
    )
    projection = FundProjection(fund={"chain_id": 84532, "fund_address": address})
    active = ActiveFund(
        "eth-usdc-csp",
        "csp",
        address,
        registry,
        projection,
        {"checkpoint": {"next_block": 100}},
    )
    monkeypatch.setattr(collector_module, "_load_events", lambda *_args: [])
    monkeypatch.setattr(
        collector_module,
        "project_events",
        lambda *_args, **_kwargs: FundProjection(
            fund={"chain_id": 84532, "fund_address": address}
        ),
    )
    topic = Web3.keccak(text="NavInvalidated(bytes32)").hex()
    raw = {
        "address": address,
        "blockNumber": hex(100),
        "transactionIndex": hex(0),
        "logIndex": hex(0),
        "blockHash": "0x" + "aa" * 32,
        "transactionHash": "0x" + "bb" * 32,
        "topics": [topic, "0x" + "11" * 32],
        "data": "0x",
    }

    ingestions = SupabaseCoordinator(client=object()).build_ingestions(
        [active], [raw], BlockHeader(101, "0x" + "aa" * 32, NOW)
    )

    assert ingestions[0]["events"][0]["event_name"] == "NavInvalidated"
    assert ingestions[0]["from_block"] == 100
    assert ingestions[0]["to_block"] == 101


def test_event_gap_becomes_explicit_backfill() -> None:
    assert event_range(99, 100) == ("foreground", 100, 100)
    assert event_range(0, MAX_EVENT_BLOCK_RANGE + 1)[0] == "backfill"


def test_migration_contains_database_time_guards_and_durable_breaker() -> None:
    sql = Path(
        "supabase/migrations/202608220001_b1n489_snapshot_collector.sql"
    ).read_text()
    lowered = sql.lower()
    assert "floor(extract(epoch from db_now) / 30)" in lowered
    assert "primary key (environment, chain_id, window_id)" in lowered
    assert "clock_timestamp() < w.publish_deadline" in lowered
    assert "p_window_id <= current_row.window_id" in lowered
    assert "p_snapshot_block < current_row.snapshot_block" in lowered
    assert "retry_call_count < 4" in lowered
    assert "failure_streak + 1 >= 3" in lowered
    assert "interval '60 seconds'" in lowered
    assert "on conflict on constraint v2_snapshot_windows_pkey do nothing" in lowered
    assert "invalid or incomplete snapshot fund payload" in lowered
    assert "snapshot funds do not exactly match enabled registry" in lowered
    assert "normalized_update_count <> payload_fund_count" in lowered
    assert "where excluded.window_id > v2_snapshot_current.window_id" in lowered
    assert "from public, anon, authenticated" in lowered
