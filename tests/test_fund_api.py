from typing import Any

import pytest
from fastapi.testclient import TestClient

import src.api.csp_vault as fund_api
from src.main import app
from src.vaults.csp_service import (
    PROXY_ROLES,
    REQUIRED_TRUSTED_ROLES,
    FundService,
)

FUND = "0xf000000000000000000000000000000000000001"
SHARE = "0xf000000000000000000000000000000000000002"
USDC = "0xf000000000000000000000000000000000000003"
WETH = "0xf000000000000000000000000000000000000004"
USER = "0xf000000000000000000000000000000000000005"


class FakeRepository:
    def __init__(self):
        self.registry = {
            "fund_key": "base-sepolia:csp",
            "chain_id": 84532,
            "fund_address": FUND,
            "share_token": SHARE,
            "accounting_asset": USDC,
            "weth": WETH,
            "deployment_status": "DEPLOYED",
            "enabled": True,
            "share_symbol": "b1CSP",
            "share_decimals": 18,
            "accounting_asset_symbol": "USDC",
            "accounting_asset_decimals": 6,
        }
        self.fund_state = {
            "net_assets": "2100",
            "share_supply": "2000",
            "virtual_shares": "100",
            "accounted_idle_assets": "500",
            "reserved_claim_assets": "100",
            "nav_stale": False,
            "reconciled": True,
            "deposits_paused": False,
            "redemptions_paused": False,
            "execution_lock_owner": None,
            "has_active_processing": False,
            "as_of_block": 100,
            "as_of_block_hash": "0x01",
            "indexed_at": "2099-07-21T00:00:00Z",
            "last_report_nonce": 2,
            "nav_valid_after_block": 90,
            "nav_valid_until_block": 110,
        }
        self.user = {"shares": "100", "redemption": {}}
        self.rows: list[dict[str, Any]] = []
        self.head = {
            "block_number": 100,
            "block_hash": "0x01",
            "observed_at": "2099-07-21T00:00:00Z",
        }
        self.bindings = [
            {
                "contract_role": role,
                "contract_address": SHARE if role == "fund_share" else FUND,
                "interface_version": 1,
                "implementation_address": FUND if role in PROXY_ROLES else None,
                "valid_from_block": 1,
                "valid_to_block": None,
            }
            for role in REQUIRED_TRUSTED_ROLES
        ]

    def registries(self):
        return [self.registry]

    def state(self, _chain, _fund):
        return self.fund_state

    def inventory(self, _chain, _fund):
        return [
            {"asset_address": USDC, "bucket": "strategy_accounted", "amount": "1500"},
            {"asset_address": WETH, "bucket": "assigned", "amount": "2"},
        ]

    def positions(self, _chain, _fund):
        return [
            {
                "position_id": "1",
                "lifecycle": "settled_otm",
                "option_amount": "10000000",
                "collateral": "300",
                "premium_earned": "7",
            },
            {
                "position_id": "2",
                "lifecycle": "open",
                "option_amount": "49230769",
                "collateral": "400",
                "premium_earned": "11",
            },
        ]

    def nav_valuation(self, _chain, _fund, report_nonce):
        assert report_nonce == 2
        return {
            "snapshot_block": 99,
            "snapshot_block_hash": "0x01",
            "reports": [
                {
                    "grossAssets": 500,
                    "liabilities": 0,
                    "baseExitCost": 0,
                },
                {
                    "grossAssets": 2_000,
                    "liabilities": 400,
                    "baseExitCost": 0,
                },
            ],
            "marks": [
                {
                    "position_id": 2,
                    "model_version": 1,
                    "methodology": "european_black_scholes",
                    "source_quality": "single_model_multi_signer",
                    "stress_liability_assets": "800",
                    "strike_price_8": "162500000000",
                    "expiry_timestamp": 1785312000,
                    "observed_at": "2099-07-21T00:00:00Z",
                }
            ],
        }

    def position(self, _chain, _fund, _wallet):
        return self.user

    def contracts(self, _chain, _fund):
        return self.bindings

    def confirmed_head(self, _chain):
        return self.head

    def activity(self, _chain, _fund, cursor, limit):
        rows = self.rows
        if cursor:
            rows = [
                row for row in rows if (row["block_number"], row["log_index"]) < cursor
            ]
        return rows[:limit]


def test_registry_summary_and_fund_inventory() -> None:
    service = FundService(FakeRepository())
    summary = service.summary("base-sepolia:csp")

    assert service.list_funds().funds[0].accounting_asset.symbol == "USDC"
    assert summary.composition.assigned_weth == "2"
    assert summary.composition.strategy_accounting_assets == "1500"
    assert summary.composition.locked_collateral_assets == "400"
    assert summary.composition.gross_assets == "2500"
    assert summary.composition.fair_option_liability_assets == "400"
    assert summary.composition.assigned_weth_value_assets == "100"
    assert summary.nav.methodology == "european_black_scholes"
    assert summary.nav.source_quality == "single_model_multi_signer"
    assert summary.stress_price_assets == str((1700 + 1) * 10**18 // 2100)
    assert summary.strategy.total_premium_collected_assets == "18"
    assert summary.strategy.latest_position is not None
    assert summary.strategy.latest_position.position_id == 2
    assert summary.strategy.latest_position.strike_price_usd_8 == "162500000000"
    assert summary.strategy.latest_position.expiry_timestamp == 1785312000
    assert summary.strategy.latest_position.option_amount_8 == "49230769"
    assert summary.strategy.latest_position.collateral_assets == "400"
    assert summary.strategy.latest_position.premium_earned_assets == "11"
    assert summary.strategy.next_open_after == 1785312000
    assert summary.strategy.next_open_condition == "after_current_settlement"
    assert summary.actions.deposit.available is True


def test_strategy_snapshot_handles_empty_fund() -> None:
    repository = FakeRepository()
    repository.positions = lambda _chain, _fund: []

    strategy = FundService(repository).summary("base-sepolia:csp").strategy

    assert strategy.latest_position is None
    assert strategy.total_premium_collected_assets == "0"
    assert strategy.next_open_after is None
    assert strategy.next_open_condition == "when_funded_and_pricing_is_ready"


def test_strategy_snapshot_uses_latest_settled_position_without_promising_a_time() -> (
    None
):
    repository = FakeRepository()
    repository.positions = lambda _chain, _fund: [
        {
            "position_id": "3",
            "lifecycle": "settled_otm",
            "option_amount": "50000000",
            "collateral": "800000000",
            "premium_earned": "61",
        }
    ]
    repository.nav_valuation = lambda _chain, _fund, _nonce: {
        "reports": [],
        "marks": [],
    }

    strategy = FundService(repository).summary("base-sepolia:csp").strategy

    assert strategy.latest_position is not None
    assert strategy.latest_position.position_id == 3
    assert strategy.latest_position.strike_price_usd_8 is None
    assert strategy.latest_position.expiry_timestamp is None
    assert strategy.total_premium_collected_assets == "61"
    assert strategy.next_open_after is None
    assert strategy.next_open_condition == "when_pricing_is_ready"


def test_disabled_incomplete_funds_are_not_listed_or_addressable() -> None:
    repository = FakeRepository()
    repository.registry.update(
        enabled=False,
        share_symbol=None,
        share_decimals=None,
        accounting_asset_symbol=None,
        accounting_asset_decimals=None,
    )
    service = FundService(repository)

    assert service.list_funds().funds == []
    with pytest.raises(LookupError):
        service.summary("base-sepolia:csp")


def test_registry_binding_mismatch_disables_writes() -> None:
    repository = FakeRepository()
    fund_vault = next(
        row for row in repository.bindings if row["contract_role"] == "fund_vault"
    )
    fund_vault["contract_address"] = USER

    config = FundService(repository).config("base-sepolia:csp")

    assert config.writes_enabled is False
    assert config.blocked_reason_code == "UNTRUSTED_BINDING"


def test_empty_fund_is_displayable_without_division_by_zero() -> None:
    repository = FakeRepository()
    repository.fund_state.update(net_assets="0", share_supply="0", virtual_shares="0")
    summary = FundService(repository).summary("base-sepolia:csp")

    assert summary.share_price_assets == "0"


def test_position_uses_exact_virtual_share_conversion_and_no_user_weth() -> None:
    position = FundService(FakeRepository()).position("base-sepolia:csp", USER)

    assert position.accounting_value == str((100 * (2100 + 1)) // (2000 + 100))
    assert "weth" not in position.model_dump_json().lower()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"nav_stale": True}, "STALE_SNAPSHOT"),
        ({"reconciled": False}, "UNRECONCILED"),
        ({"execution_lock_owner": USER}, "EXECUTION_LOCKED"),
        ({"deposits_paused": True}, "DEPOSITS_PAUSED"),
        ({"has_active_processing": True}, "FLOW_PROCESSING"),
    ],
)
def test_display_survives_while_actions_fail_closed(change, reason) -> None:
    repository = FakeRepository()
    repository.fund_state.update(change)
    summary = FundService(repository).summary("base-sepolia:csp")

    assert summary.net_assets == "2100"
    assert reason in {
        summary.actions.deposit.reason_code,
        summary.actions.request_redemption.reason_code,
    }


def test_unknown_deployment_and_redemption_next_actions() -> None:
    repository = FakeRepository()
    repository.registry["deployment_status"] = "NOT_DEPLOYED"
    repository.user["redemption"] = {
        "pending_shares": "20",
        "claimable_shares": "10",
        "claimable_assets": "11",
        "status": "claimable",
    }
    service = FundService(repository)

    assert (
        service.summary("base-sepolia:csp").actions.deposit.reason_code
        == "MISSING_TRUSTED_DEPLOYMENT"
    )
    assert service.position("base-sepolia:csp", USER).redemption.next_action == "claim"


def test_no_pending_or_claimable_redemption_reasons() -> None:
    position = FundService(FakeRepository()).position("base-sepolia:csp", USER)

    assert position.actions.cancel_redemption.reason_code == "NO_PENDING_REDEMPTION"
    assert position.actions.claim_redemption.reason_code == "NO_CLAIMABLE_REDEMPTION"


def test_cancellation_remains_available_while_redemptions_are_paused() -> None:
    repository = FakeRepository()
    repository.fund_state["redemptions_paused"] = True
    repository.user["redemption"] = {
        "pending_shares": "20",
        "claimable_shares": "0",
        "claimable_assets": "0",
        "status": "pending",
    }
    position = FundService(repository).position("base-sepolia:csp", USER)

    assert position.actions.request_redemption.reason_code == "REDEMPTIONS_PAUSED"
    assert position.actions.cancel_redemption.available is True


def test_unrelated_processing_batch_does_not_block_cancellation() -> None:
    repository = FakeRepository()
    repository.fund_state["has_active_processing"] = True
    repository.user["redemption"] = {
        "pending_shares": "20",
        "claimable_shares": "0",
        "claimable_assets": "0",
        "status": "pending",
    }

    actions = FundService(repository).position("base-sepolia:csp", USER).actions
    assert actions.cancel_redemption.available is True


@pytest.mark.parametrize(
    "field", ["latest_batch_processing", "latest_batch_unwind_committed"]
)
def test_latest_batch_processing_or_unwind_blocks_cancellation(field) -> None:
    repository = FakeRepository()
    repository.user["redemption"] = {
        "pending_shares": "20",
        "claimable_shares": "0",
        "claimable_assets": "0",
        "status": "pending",
        field: True,
    }

    actions = FundService(repository).position("base-sepolia:csp", USER).actions
    assert actions.cancel_redemption.reason_code == "FLOW_PROCESSING"


def test_execution_lock_blocks_cancellation() -> None:
    repository = FakeRepository()
    repository.fund_state["execution_lock_owner"] = USER
    repository.user["redemption"] = {
        "pending_shares": "20",
        "claimable_shares": "0",
        "claimable_assets": "0",
        "status": "pending",
    }

    actions = FundService(repository).position("base-sepolia:csp", USER).actions
    assert actions.cancel_redemption.reason_code == "EXECUTION_LOCKED"


def test_cancellation_blocks_when_claimable_redemption_exists() -> None:
    repository = FakeRepository()
    repository.user["redemption"] = {
        "pending_shares": "20",
        "claimable_shares": "1",
        "claimable_assets": "2",
        "status": "claimable",
    }

    actions = FundService(repository).position("base-sepolia:csp", USER).actions
    assert actions.cancel_redemption.reason_code == "CLAIMABLE_REDEMPTION_EXISTS"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda repo: repo.bindings.clear(), "MISSING_TRUSTED_DEPLOYMENT"),
        (
            lambda repo: repo.bindings[0].update(interface_version=2),
            "UNSUPPORTED_INTERFACE",
        ),
        (
            lambda repo: next(
                row for row in repo.bindings if row["contract_role"] in PROXY_ROLES
            ).update(implementation_address=None),
            "UNTRUSTED_IMPLEMENTATION",
        ),
        (
            lambda repo: repo.fund_state.update(indexed_at="2020-01-01T00:00:00Z"),
            "STALE_INDEXER_LEASE",
        ),
        (
            lambda repo: repo.fund_state.update(nav_valid_until_block=99),
            "STALE_NAV_WINDOW",
        ),
        (
            lambda repo: repo.head.update(observed_at="2020-01-01T00:00:00Z"),
            "STALE_CONFIRMED_HEAD",
        ),
        (lambda repo: setattr(repo, "head", None), "UNKNOWN_CONFIRMED_HEAD"),
    ],
)
def test_every_write_requires_trusted_fresh_state(mutation, reason) -> None:
    repository = FakeRepository()
    mutation(repository)
    actions = FundService(repository).summary("base-sepolia:csp").actions

    reasons = {
        actions.deposit.reason_code,
        actions.request_redemption.reason_code,
        actions.cancel_redemption.reason_code,
        actions.claim_redemption.reason_code,
    }
    assert reasons == {reason}


def test_activity_cursor_is_stable_and_payload_is_allowlisted() -> None:
    repository = FakeRepository()
    repository.rows = [
        {
            "activity_type": "deposit",
            "transaction_hash": f"0x{i}",
            "block_number": 100 - i,
            "log_index": 3 - i,
            "wallet_address": USER,
            "payload": {"assets": i + 1, "privateField": "hidden"},
        }
        for i in range(3)
    ]
    service = FundService(repository)
    first = service.activity("base-sepolia:csp", None, 2)
    second = service.activity("base-sepolia:csp", first.next_cursor, 2)

    assert [item.transaction_hash for item in first.items] == ["0x0", "0x1"]
    assert [item.transaction_hash for item in second.items] == ["0x2"]
    assert first.items[0].details == {"assets": 1}


def test_compact_routes_validate_addresses_and_cache(monkeypatch) -> None:
    monkeypatch.setattr(fund_api, "_service", FundService(FakeRepository()))
    client = TestClient(app)

    first = client.get("/v2/vaults/base-sepolia:csp")
    cached = client.get(
        "/v2/vaults/base-sepolia:csp",
        headers={"If-None-Match": first.headers["etag"]},
    )
    position = client.get(f"/v2/vaults/base-sepolia:csp/positions/{USER}")
    invalid = client.get("/v2/vaults/base-sepolia:csp/positions/not-an-address")
    invalid_key = client.get("/v2/vaults/INVALID!")

    assert first.status_code == 200 and cached.status_code == 304
    assert first.headers["cache-control"] == "public, no-cache"
    assert position.headers["cache-control"] == "private, no-cache"
    assert invalid.status_code == 400
    assert invalid_key.status_code == 400
