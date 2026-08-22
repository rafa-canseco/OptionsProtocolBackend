import json
from pathlib import Path
from types import SimpleNamespace

from eth_abi import decode, encode
from fastapi.testclient import TestClient

import src.api.mm_routes as mm_routes
from src.api.deps import require_mm_api_key
from src.fund_indexer.mm_snapshot import (
    BATCH,
    CC_ADAPTER_CONFIG,
    CC_ADAPTER_STATE,
    CSP_ADAPTER_CONFIG,
    CSP_ADAPTER_STATE,
    NAV,
    STRATEGY_CONFIG,
    PlanResult,
    _json_value,
    decode_covered_call,
    decode_csp,
)
from src.main import app


class Result:
    def __init__(self, data):
        self.data = data


class Client:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.params = None

    def rpc(self, name, params):
        assert name == "v2_get_current_snapshot"
        self.params = params
        return self

    def execute(self):
        if self.error:
            raise self.error
        return Result(self.data)


def payload():
    raw = json.loads(Path("tests/fixtures/rpc_snapshot_envelope.json").read_text())
    raw["environment"] = "staging"
    return raw


def test_authenticated_snapshot_is_one_atomic_database_read(monkeypatch) -> None:
    raw = payload()
    database = Client(raw)
    monkeypatch.setattr(mm_routes, "get_client", lambda: database)
    app.dependency_overrides[require_mm_api_key] = lambda: raw["common"][
        "market_maker"
    ]["mm_address"]
    try:
        response = TestClient(app).get(
            "/mm/snapshot?environment=staging&chain_id=84532"
        )
    finally:
        app.dependency_overrides.pop(require_mm_api_key, None)

    assert response.status_code == 200
    assert response.json() == raw
    assert database.params == {"p_environment": "staging", "p_chain_id": 84532}


def _abi_json(abi_type: str, value):
    return _json_value(decode([abi_type], encode([abi_type], [value]))[0])


def test_csp_decoder_emits_exact_inactive_position_abi_shape() -> None:
    raw = payload()
    canonical = raw["funds"][0]["state"]
    allocator = canonical["allocator"]
    operations = canonical["operations"]
    nav = allocator["nav"]
    nav_value = tuple(nav[:10]) + tuple(
        bytes.fromhex(nav[index][2:]) if index in {10, 11, 12, 14} else nav[index]
        for index in range(10, 15)
    )
    config = allocator["adapter_config"]
    adapter_state = allocator["adapter_state"]
    batch = operations["batch"]
    names = [f"policy_{index}" for index in range(6)]
    values = {
        "nav": _abi_json(NAV, nav_value),
        "strategy_hash": allocator["strategy_hash"],
        "strategy_config": _abi_json(
            STRATEGY_CONFIG, tuple(allocator["strategy_config"])
        ),
        "adapter_config": _abi_json(
            CSP_ADAPTER_CONFIG, (tuple(config[0]), config[1], config[2])
        ),
        "adapter_state": _abi_json(
            CSP_ADAPTER_STATE,
            (adapter_state[0], bytes.fromhex(adapter_state[1][2:]), *adapter_state[2:]),
        ),
        "treasury": allocator["treasury"],
        "total_assets": allocator["total_assets"],
        "idle_assets": allocator["idle_assets"],
        "allocated": allocator["allocated"],
        "minimum_idle_bps": allocator["minimum_idle_bps"],
        "processing": allocator["processing"],
        "pending_shares": allocator["pending_shares"],
        "protocol_fee_bps": allocator["protocol_fee_bps"],
        "next_batch_id": operations["batch_id"],
        "open_batch_id": operations["open_batch_id"],
        "share_supply": operations["eligible_supply"],
        "virtual_shares": operations["virtual_shares"],
        "exit_policy": [0, operations["max_window_outflow_bps"]],
        f"batch.{operations['batch_id']}": _abi_json(
            BATCH,
            tuple(batch[:12]) + (bytes.fromhex(batch[12][2:]),) + tuple(batch[13:]),
        ),
        f"outflow.{nav[9]}": [
            operations["window_eligible_supply"],
            operations["window_processed_shares"],
        ],
    }
    values.update(
        {
            f"valuation.{name}": value
            for name, value in zip(names, allocator["valuation_policy"], strict=True)
        }
    )
    fund = SimpleNamespace(
        registry=SimpleNamespace(
            accounting_asset="0x" + "22" * 20,
            quote_asset="0x" + "22" * 20,
            weth="0x" + "55" * 20,
        ),
        inputs={"valuator_names": names},
        projection=SimpleNamespace(positions={}),
    )
    decoded_state, _ = decode_csp(
        fund,
        PlanResult(values, len(values)),
        {"positions": [], "quotes": [], "roles": {}},
        include_common=False,
    )
    raw["funds"][0]["state"] = decoded_state

    envelope = mm_routes.SnapshotEnvelope.model_validate(raw)
    position = envelope.funds[0].state["allocator"]["position"]
    assert len(position) == 13
    assert position[:2] == ["0x" + "00" * 20] * 2
    assert position[12] == "0x" + "00" * 32


def test_real_abi_decode_flows_through_snapshot_model_and_endpoint(monkeypatch) -> None:
    raw = payload()
    canonical = raw["funds"][1]["state"]
    allocator = canonical["allocator"]
    operations = canonical["operations"]
    nav = allocator["nav"]
    nav_value = tuple(nav[:10]) + tuple(
        bytes.fromhex(nav[index][2:]) if index in {10, 11, 12, 14} else nav[index]
        for index in range(10, 15)
    )
    config = allocator["adapter_config"]
    strategy_config = allocator["strategy_config"]
    adapter_state = allocator["adapter_state"]
    batch = operations["batch"]
    names = [f"policy_{index}" for index in range(10)]
    values = {
        "nav": _abi_json(NAV, nav_value),
        "strategy_hash": allocator["strategy_hash"],
        "strategy_config": _abi_json(STRATEGY_CONFIG, tuple(strategy_config)),
        "adapter_config": _abi_json(
            CC_ADAPTER_CONFIG,
            (tuple(config[0]), config[1], config[2]),
        ),
        "adapter_state": _abi_json(
            CC_ADAPTER_STATE,
            (adapter_state[0], bytes.fromhex(adapter_state[1][2:]), *adapter_state[2:]),
        ),
        "total_assets": allocator["total_assets"],
        "idle_assets": allocator["idle_assets"],
        "allocated": allocator["allocated"],
        "minimum_idle_bps": allocator["minimum_idle_bps"],
        "processing": allocator["processing"],
        "pending_shares": allocator["pending_shares"],
        "protocol_fee_bps": allocator["protocol_fee_bps"],
        "spot_price": allocator["spot_price"],
        "next_batch_id": operations["batch_id"],
        "open_batch_id": operations["open_batch_id"],
        "share_supply": operations["eligible_supply"],
        "virtual_shares": operations["virtual_shares"],
        "exit_policy": [0, operations["max_window_outflow_bps"]],
        f"batch.{operations['batch_id']}": _abi_json(
            BATCH,
            tuple(batch[:12]) + (bytes.fromhex(batch[12][2:]),) + tuple(batch[13:]),
        ),
        f"outflow.{nav[9]}": [
            operations["window_eligible_supply"],
            operations["window_processed_shares"],
        ],
    }
    values.update(
        {
            f"valuation.{name}": value
            for name, value in zip(names, allocator["valuation_policy"], strict=True)
        }
    )
    values.update(
        {
            f"observer.{index}": value
            for index, value in enumerate(allocator["valuation_observers"])
        }
    )
    fund = SimpleNamespace(
        registry=SimpleNamespace(
            accounting_asset="0x" + "55" * 20,
            quote_asset="0x" + "22" * 20,
            weth="0x" + "55" * 20,
        ),
        inputs={"valuator_names": names},
        projection=SimpleNamespace(positions={}),
    )
    decoded_state, _ = decode_covered_call(
        fund,
        PlanResult(values, len(values)),
        {"positions": [], "quotes": [], "observers": [object(), object()]},
        include_common=False,
    )
    raw["funds"][1]["state"] = decoded_state
    database = Client(raw)
    monkeypatch.setattr(mm_routes, "get_client", lambda: database)
    app.dependency_overrides[require_mm_api_key] = lambda: raw["common"][
        "market_maker"
    ]["mm_address"]
    try:
        response = TestClient(app).get(
            "/mm/snapshot?environment=staging&chain_id=84532"
        )
    finally:
        app.dependency_overrides.pop(require_mm_api_key, None)

    assert response.status_code == 200, response.text
    state = response.json()["funds"][1]["state"]
    assert state["allocator"]["nav"][12].startswith("0x")
    assert state["allocator"]["adapter_config"][1] == config[1]
    assert state["operations"]["batch"][12].startswith("0x")
    assert state["operations"]["batch"][18:] == [True, True, False, False]


def test_snapshot_database_failure_has_no_rpc_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        mm_routes, "get_client", lambda: Client(error=RuntimeError("unavailable"))
    )
    app.dependency_overrides[require_mm_api_key] = lambda: "0x" + "11" * 20
    try:
        response = TestClient(app).get(
            "/mm/snapshot?environment=staging&chain_id=84532"
        )
    finally:
        app.dependency_overrides.pop(require_mm_api_key, None)

    assert response.status_code == 503
    assert response.json() == {"detail": "Snapshot unavailable"}


def test_snapshot_signer_mismatch_is_rejected(monkeypatch) -> None:
    raw = payload()
    monkeypatch.setattr(mm_routes, "get_client", lambda: Client(raw))
    app.dependency_overrides[require_mm_api_key] = lambda: "0x" + "99" * 20
    try:
        response = TestClient(app).get(
            "/mm/snapshot?environment=staging&chain_id=84532"
        )
    finally:
        app.dependency_overrides.pop(require_mm_api_key, None)
    assert response.status_code == 403
