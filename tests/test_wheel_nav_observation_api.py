from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3 import Web3

import src.api.csp_vault as fund_api
import src.vaults.csp_service as fund_service
from src.api.deps import require_mm_api_key
from src.fund_nav.models import IDLE_COMPONENT_ID
from src.vaults.csp_service import (
    FundService,
    SupabaseFundRepository,
    WheelNavObservationError,
)


CHAIN_ID = 84532
FUND = "0xf000000000000000000000000000000000000001"
COORDINATOR = "0xf000000000000000000000000000000000000002"
CSP_LANE = "0xf000000000000000000000000000000000000010"
CALL_LANE = "0xf000000000000000000000000000000000000020"
SNAPSHOT_BLOCK = 100
SNAPSHOT_HASH = "0x" + "11" * 32
TX_HASH = "0x" + "22" * 32
COORDINATOR_HASH = "0x" + "33" * 32
IDLE_HASH = "0x" + "44" * 32


def _component_id() -> str:
    return Web3.to_hex(
        Web3.solidity_keccak(
            ["string", "address"],
            ["STRATEGY", Web3.to_checksum_address(COORDINATOR)],
        )
    ).lower()


def _lane_report(lane: str, strategy_kind: str, index: int) -> dict:
    return {
        "child_vault": lane,
        "strategy_kind": strategy_kind,
        "custody_domain": f"0xf0000000000000000000000000000000000000{30 + index:02x}",
        "snapshot_block": SNAPSHOT_BLOCK,
        "snapshot_block_hash": SNAPSHOT_HASH,
        "valid_after_block": 95,
        "valid_until_block": 105,
        "child_shares": str(100 + index),
        "position_state_hash": "0x" + f"{50 + index:02x}" * 32,
        "expected_position_state_hash": "0x" + f"{50 + index:02x}" * 32,
        "gross_assets_usdc": str(1_000_000 + index),
        "liabilities_usdc": str(100_000 + index),
        "liquid_usdc": str(500_000 + index),
        "base_exit_cost_usdc": str(10_000 + index),
        "data_hash": "0x" + f"{60 + index:02x}" * 32,
        "valuation_data": "0x1234",
    }


def _component_report(component_id: str, position_hash: str) -> dict:
    return {
        "fund": FUND,
        "componentId": component_id,
        "chainId": CHAIN_ID,
        "snapshotBlock": SNAPSHOT_BLOCK,
        "snapshotBlockHash": SNAPSHOT_HASH,
        "validAfterBlock": 101,
        "validUntilBlock": 120,
        "reporterSetVersion": 1,
        "componentNonce": 7,
        "positionStateHash": position_hash,
        "grossAssets": 2_000_000,
        "liabilities": 200_000,
        "liquidAccountingAssets": 1_000_000,
        "baseExitCost": 20_000,
        "dataHash": "0x" + "55" * 32,
    }


class ObservationRepository:
    def __init__(self, *, strategy_kind: str = "meta_wheel") -> None:
        self.registry = {
            "fund_key": "base-sepolia:meta-wheel",
            "strategy_kind": strategy_kind,
            "chain_id": CHAIN_ID,
            "fund_address": FUND,
            "enabled": True,
        }
        csp = _lane_report(CSP_LANE, "csp", 0)
        call = _lane_report(CALL_LANE, "covered_call", 1)
        self.rows = {
            "nav": {
                "report_nonce": 7,
                "snapshot_block": SNAPSHOT_BLOCK,
                "snapshot_block_hash": SNAPSHOT_HASH,
                "coherent": True,
                "child_reports": [csp, call],
            },
            "run": {
                "report_nonce": 7,
                "snapshot_block": SNAPSHOT_BLOCK,
                "snapshot_block_hash": SNAPSHOT_HASH,
                "status": "confirmed",
                "transaction_hash": TX_HASH,
                "reports": [
                    _component_report(
                        Web3.to_hex(IDLE_COMPONENT_ID).lower(), IDLE_HASH
                    ),
                    _component_report(_component_id(), COORDINATOR_HASH),
                ],
            },
            "lane_registry": [
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
            ],
            "valuations": [
                {
                    key: value
                    for key, value in csp.items()
                    if not key.startswith("expected_")
                },
                {
                    key: value
                    for key, value in call.items()
                    if not key.startswith("expected_")
                },
            ],
            "contracts": [
                {
                    "contract_role": "wheel_coordinator",
                    "contract_address": COORDINATOR,
                    "implementation_address": "0xf000000000000000000000000000000000000003",
                    "interface_version": 1,
                    "valid_from_block": 1,
                    "valid_to_block": None,
                }
            ],
            "confirmed_head": {
                "block_number": SNAPSHOT_BLOCK,
                "block_hash": SNAPSHOT_HASH,
                "observed_at": "2099-08-01T00:00:00Z",
            },
            "chain_binding": {
                "chain_id": CHAIN_ID,
                "snapshot_block": SNAPSHOT_BLOCK,
                "snapshot_block_hash": SNAPSHOT_HASH,
                "transaction_hash": TX_HASH,
                "receipt_status": 1,
                "receipt_block": 102,
                "receipt_block_hash": "0x" + "66" * 32,
                "canonical_receipt_block_hash": "0x" + "66" * 32,
            },
        }

    def registries(self) -> list[dict]:
        return [self.registry]

    def wheel_nav_observation_rows(
        self, chain_id: int, fund: str, snapshot_block: int
    ) -> dict:
        assert (chain_id, fund, snapshot_block) == (
            CHAIN_ID,
            FUND,
            SNAPSHOT_BLOCK,
        )
        return deepcopy(self.rows)

    def wheel_nav_chain_binding(
        self, chain_id: int, snapshot_block: int, transaction_hash: str
    ) -> dict:
        assert (chain_id, snapshot_block, transaction_hash) == (
            CHAIN_ID,
            SNAPSHOT_BLOCK,
            TX_HASH,
        )
        return deepcopy(self.rows["chain_binding"])


def _client(
    repository: ObservationRepository, monkeypatch
) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    app.include_router(fund_api.router)
    app.dependency_overrides[require_mm_api_key] = lambda: COORDINATOR
    monkeypatch.setattr(fund_api, "_service", FundService(repository))
    return app, TestClient(app)


def test_authenticated_mm_gets_exact_confirmed_wheel_nav_observation(
    monkeypatch,
) -> None:
    app, client = _client(ObservationRepository(), monkeypatch)

    response = client.get(
        "/v2/vaults/base-sepolia:meta-wheel/wheel/nav-observation",
        params={"snapshot_block": SNAPSHOT_BLOCK},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.json() == {
        "fundKey": "base-sepolia:meta-wheel",
        "chainId": CHAIN_ID,
        "fundAddress": FUND,
        "coordinator": COORDINATOR,
        "reportNonce": 7,
        "componentId": _component_id(),
        "coordinatorPositionStateHash": COORDINATOR_HASH,
        "snapshotBlock": SNAPSHOT_BLOCK,
        "snapshotBlockHash": SNAPSHOT_HASH,
        "validAfterBlock": 101,
        "validUntilBlock": 120,
        "lanes": [
            {
                "lane": CSP_LANE,
                "childShares": "100",
                "positionStateHash": "0x" + "32" * 32,
                "snapshotBlock": SNAPSHOT_BLOCK,
                "snapshotBlockHash": SNAPSHOT_HASH,
                "validAfterBlock": 95,
                "validUntilBlock": 105,
            },
            {
                "lane": CALL_LANE,
                "childShares": "101",
                "positionStateHash": "0x" + "33" * 32,
                "snapshotBlock": SNAPSHOT_BLOCK,
                "snapshotBlockHash": SNAPSHOT_HASH,
                "validAfterBlock": 95,
                "validUntilBlock": 105,
            },
        ],
    }
    app.dependency_overrides.clear()


def test_wheel_nav_observation_requires_mm_authentication() -> None:
    app = FastAPI()
    app.include_router(fund_api.router)

    response = TestClient(app).get(
        "/v2/vaults/base-sepolia:meta-wheel/wheel/nav-observation",
        params={"snapshot_block": SNAPSHOT_BLOCK},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda rows: rows["nav"]["child_reports"].append(
                deepcopy(rows["nav"]["child_reports"][0])
            ),
            "WHEEL_NAV_LANE_SET_INVALID",
        ),
        (
            lambda rows: rows["valuations"].pop(),
            "WHEEL_NAV_LANE_SET_INVALID",
        ),
        (
            lambda rows: rows["valuations"][0].update(child_shares="999"),
            "WHEEL_NAV_LANE_BINDING_INVALID",
        ),
        (
            lambda rows: rows["confirmed_head"].update(block_hash="0x" + "99" * 32),
            "WHEEL_NAV_SNAPSHOT_NOT_CANONICAL",
        ),
        (
            lambda rows: rows["run"]["reports"].append(
                deepcopy(rows["run"]["reports"][1])
            ),
            "WHEEL_NAV_REPORT_BINDING_INVALID",
        ),
        (
            lambda rows: (
                rows["confirmed_head"].update(block_number=110),
                rows["chain_binding"].update(snapshot_block_hash="0x" + "99" * 32),
            ),
            "WHEEL_NAV_CHAIN_BINDING_INVALID",
        ),
    ],
)
def test_wheel_nav_observation_fails_closed_on_incoherent_rows(
    mutate, expected_code: str, monkeypatch
) -> None:
    repository = ObservationRepository()
    mutate(repository.rows)
    app, client = _client(repository, monkeypatch)

    response = client.get(
        "/v2/vaults/base-sepolia:meta-wheel/wheel/nav-observation",
        params={"snapshot_block": SNAPSHOT_BLOCK},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": expected_code}
    app.dependency_overrides.clear()


def test_standalone_fund_cannot_use_wheel_observation_path(monkeypatch) -> None:
    repository = ObservationRepository(strategy_kind="csp")
    app, client = _client(repository, monkeypatch)

    response = client.get(
        "/v2/vaults/base-sepolia:meta-wheel/wheel/nav-observation",
        params={"snapshot_block": SNAPSHOT_BLOCK},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "FUND_IS_NOT_META_WHEEL"}
    app.dependency_overrides.clear()


def test_registered_idle_lanes_are_omitted_without_blocking_observation(
    monkeypatch,
) -> None:
    repository = ObservationRepository()
    repository.rows["nav"]["child_reports"] = []
    repository.rows["valuations"] = []
    app, client = _client(repository, monkeypatch)

    response = client.get(
        "/v2/vaults/base-sepolia:meta-wheel/wheel/nav-observation",
        params={"snapshot_block": SNAPSHOT_BLOCK},
    )

    assert response.status_code == 200
    assert response.json()["lanes"] == []
    app.dependency_overrides.clear()


class _Query:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def select(self, _fields: str):
        return self

    def eq(self, _field: str, _value):
        return self

    def execute(self):
        return type("Result", (), {"data": self.rows})()


class _DuplicateSnapshotClient:
    def table(self, table: str) -> _Query:
        assert table == "v2_meta_wheel_nav_snapshots"
        return _Query(
            [
                {"report_nonce": 1},
                {"report_nonce": 2},
            ]
        )


def test_repository_rejects_ambiguous_snapshot_before_composition(monkeypatch) -> None:
    monkeypatch.setattr(fund_service, "get_client", lambda: _DuplicateSnapshotClient())

    with pytest.raises(WheelNavObservationError) as error:
        SupabaseFundRepository().wheel_nav_observation_rows(
            CHAIN_ID, FUND, SNAPSHOT_BLOCK
        )

    assert error.value.code == "WHEEL_NAV_SNAPSHOT_NOT_CANONICAL"
