from types import SimpleNamespace

import pytest

from src.fund_indexer import backfill
from src.fund_indexer.backfill import run_once


class Result:
    def __init__(self, data):
        self.data = data


class Client:
    def __init__(self, reason="integration"):
        self.calls = []
        self.reason = reason

    def rpc(self, name, params):
        self.calls.append((name, params))
        self.name = name
        return self

    def execute(self):
        if self.name == "v2_claim_snapshot_event_backfill":
            return Result(
                [
                    {
                        "environment": "test",
                        "chain_id": 84532,
                        "from_block": 10,
                        "to_block": 11,
                        "reason": self.reason,
                    }
                ]
            )
        return Result(True)


class Coordinator:
    def __init__(self, reason="integration"):
        self.client = Client(reason)
        self.fund = SimpleNamespace(fund_address="0x" + "44" * 20, inputs={})

    def funds(self, _chain_id):
        return [self.fund]

    def build_ingestions(self, _funds, _logs, header):
        return [
            {
                "chain_id": 84532,
                "fund_address": "0x" + "44" * 20,
                "indexer_name": "tokenized_csp_fund",
                "from_block": 10,
                "to_block": header.number,
                "last_block_hash": header.hash,
                "events": [],
                "projection": {},
            }
        ]


class Provider:
    def make_request(self, method, params):
        assert method == "eth_getBlockByNumber"
        return {
            "result": {
                "hash": "0x" + "aa" * 32,
                "timestamp": hex(1_700_000_000),
            }
        }


class RPC:
    def __init__(self, chain_id=84532):
        self.w3 = SimpleNamespace(provider=Provider())
        self.chain_id = chain_id
        self.validate_calls = 0
        self.validated = False
        self.deadlines = []
        self.log_calls = 0

    def validate_chain(self, chain_id):
        if self.validated:
            return
        self.validate_calls += 1
        if self.chain_id != chain_id:
            raise RuntimeError("provider details must be hidden")
        self.validated = True

    def set_deadline(self, deadline):
        self.deadlines.append(deadline)

    def event_logs(self, _funds, start, end, *, traffic_class="recurrent_rpc"):
        assert (start, end) == (10, 11)
        assert traffic_class == "backfill_rpc"
        self.log_calls += 1
        return []


def test_standalone_backfill_rejects_public_rpc_before_db_access(monkeypatch) -> None:
    monkeypatch.setattr(
        backfill,
        "get_tokenized_fund_rpc_url",
        lambda: "https://baserpcgateway-production.up.railway.app",
    )
    monkeypatch.setattr(
        backfill,
        "SupabaseCoordinator",
        lambda: pytest.fail("DB accessed before RPC policy validation"),
    )

    with pytest.raises(ValueError, match="private authenticated Base RPC"):
        backfill.main()


def test_backfill_wrong_chain_fails_before_work_claim() -> None:
    coordinator = Coordinator()
    rpc = RPC(chain_id=8453)

    try:
        run_once(coordinator, rpc, "test", 84532)
    except RuntimeError as error:
        assert str(error) == "Backfill startup RPC validation failed"
    else:
        raise AssertionError("wrong-chain backfill must fail")

    assert rpc.validate_calls == 1
    assert coordinator.client.calls == []


def test_reorg_backfill_keeps_pre_rewind_descriptors(monkeypatch) -> None:
    coordinator = Coordinator("checkpoint_reorg_rebuild")
    rpc = RPC()
    clock = iter([0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr("src.fund_indexer.backfill.time.monotonic", lambda: next(clock))

    assert run_once(coordinator, rpc, "test", 84532, sleep=lambda _delay: None)
    names = [name for name, _ in coordinator.client.calls]
    assert names[1] == "v2_rewind_snapshot_event_backfill"
    assert coordinator.fund.inputs["checkpoint"]["next_block"] == 12


def test_explicit_backfill_executes_and_marks_work_done(monkeypatch) -> None:
    coordinator = Coordinator()
    rpc = RPC()
    clock = iter([0.0] * 8)
    monkeypatch.setattr("src.fund_indexer.backfill.time.monotonic", lambda: next(clock))
    sleeps = []

    assert run_once(coordinator, rpc, "test", 84532, sleep=sleeps.append) is True
    assert run_once(coordinator, rpc, "test", 84532, sleep=sleeps.append) is True
    assert rpc.validate_calls == 1
    assert rpc.deadlines and rpc.log_calls == 2
    assert sleeps == [1.0, 1.0]
    name, payload = coordinator.client.calls[-1]
    assert name == "v2_apply_snapshot_event_backfill_chunk"
    assert payload["p_final_chunk"] is True
    assert len(payload["p_ingestions"]) == 1
