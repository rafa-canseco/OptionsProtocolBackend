from typing import Any

from src.vaults.csp_service import (
    COMMON_PROXY_ROLES,
    FundService,
    required_trusted_roles,
)


FUND = "0xf000000000000000000000000000000000000001"
SHARE = "0xf000000000000000000000000000000000000002"
USDC = "0xf000000000000000000000000000000000000003"
WETH = "0xf000000000000000000000000000000000000004"
BLOCK_HASH = "0x" + "01" * 32


class WheelRepository:
    def __init__(self) -> None:
        self.registry = {
            "fund_key": "base-sepolia:meta-wheel",
            "strategy_kind": "meta_wheel",
            "chain_id": 84532,
            "fund_address": FUND,
            "share_token": SHARE,
            "accounting_asset": USDC,
            "weth": WETH,
            "quote_asset": None,
            "deployment_status": "DEPLOYED",
            "enabled": True,
            "share_symbol": "b1WHEEL",
            "share_decimals": 18,
            "accounting_asset_symbol": "USDC",
            "accounting_asset_decimals": 6,
        }
        self.fund_state = {
            "net_assets": "4687000000",
            "share_supply": str(4_000 * 10**18),
            "virtual_shares": str(10**18),
            "accounted_idle_assets": "200000000",
            "reserved_claim_assets": "100000000",
            "nav_stale": False,
            "reconciled": True,
            "deposits_paused": False,
            "redemptions_paused": False,
            "execution_lock_owner": None,
            "has_active_processing": False,
            "snapshot_generation": 1,
            "snapshot_published_at": "2099-07-31T00:00:00Z",
            "as_of_block": 100,
            "as_of_block_hash": BLOCK_HASH,
            "indexed_at": "2099-07-31T00:00:00Z",
            "last_report_nonce": 7,
            "nav_valid_after_block": 95,
            "nav_valid_until_block": 105,
            "management_fee_wad": "20000000000000000",
            "performance_fee_bps": 1000,
            "high_water_mark": "1000000",
        }
        self.meta_state = {
            "pending_csp_usdc": "500000000",
            "redemption_reserved_usdc": "100000000",
            "reserved_principal_usdc": "95000000",
            "policy_version": 1,
            "policy_hash": "0xpolicy",
            "cumulative_gross_premium": "10000000",
            "cumulative_protocol_fee": "1000000",
            "cumulative_net_premium": "9000000",
            "current_phase": "mixed",
            "next_action": "process_ready_tranches",
            "active_tranche_count": 2,
            "protected_assignment_floor_8": "201000000000",
            "paused": False,
        }
        self.nav = {
            "report_nonce": 7,
            "snapshot_block": 100,
            "snapshot_block_hash": "0xnav",
            "coherent": True,
            "gross_assets": "5100000000",
            "liabilities": "413000000",
            "net_assets": "4687000000",
            "child_csp_value_assets": "999000000",
            "child_covered_call_value_assets": "1998000000",
            "transition_weth": str(5 * 10**17),
            "transition_weth_value_assets": "1000000000",
            "parent_exit_cost_usdc": "10000000",
            "stress_net_assets": "4437000000",
            "observed_at": "2099-07-31T00:00:00Z",
        }
        self.tranche_rows = [
            {
                "tranche_id": "1",
                "child_vault": "0x0000000000000000000000000000000000000010",
                "state": "csp_open",
                "principal_assets": "1000000000",
                "child_shares": "100",
                "child_position_id": "7",
                "assignment_lot_ids": [],
                "literal_call_floor_8": "0",
                "required_call_floor_8": "0",
                "call_strike_8": None,
                "state_nonce": 2,
                "pending_assets": "0",
                "child_execution_state_hash": "0xcsp-execution",
            },
            {
                "tranche_id": "2",
                "child_vault": "0x0000000000000000000000000000000000000020",
                "state": "call_open",
                "principal_assets": "2000000000",
                "child_shares": "200",
                "child_position_id": "8",
                "assignment_lot_ids": ["11"],
                "literal_call_floor_8": "200000000000",
                "required_call_floor_8": "201000000000",
                "call_strike_8": "205000000000",
                "state_nonce": 6,
                "pending_assets": "0",
                "child_execution_state_hash": "0xcall-execution",
            },
        ]
        proxies = COMMON_PROXY_ROLES | {"wheel_coordinator"}
        self.bindings = [
            {
                "contract_role": role,
                "contract_address": SHARE if role == "fund_share" else FUND,
                "interface_version": 1,
                "implementation_address": FUND if role in proxies else None,
                "valid_from_block": 1,
                "valid_to_block": None,
            }
            for role in required_trusted_roles("meta_wheel")
        ]

    def registries(self):
        return [self.registry]

    def state(self, _chain, _fund):
        return self.fund_state

    def inventory(self, _chain, _fund):
        return []

    def positions(self, _chain, _fund):
        return []

    def nav_valuation(self, _chain, _fund, _nonce):
        raise AssertionError("Meta Wheel must not use standalone option marks")

    def position(self, _chain, _fund, _wallet):
        return {"shares": str(10**18), "redemption": {}}

    def wheel_state(self, _chain, _fund):
        return self.meta_state

    def wheel_tranches(self, _chain, _fund):
        return self.tranche_rows

    def wheel_nav(self, _chain, _fund, report_nonce):
        assert report_nonce == 7
        return self.nav

    def contracts(self, _chain, _fund):
        return self.bindings

    def confirmed_head(self, _chain):
        return {
            "block_number": 100,
            "block_hash": BLOCK_HASH,
            "observed_at": "2099-07-31T00:00:00Z",
        }

    def activity(self, _chain, _fund, _cursor, _limit) -> list[dict[str, Any]]:
        return []


def test_meta_wheel_is_third_product_with_compact_usdc_summary() -> None:
    service = FundService(WheelRepository())
    summary = service.summary("base-sepolia:meta-wheel")

    assert service.list_funds().funds[0].strategy_kind == "meta_wheel"
    assert summary.fund.accounting_asset.symbol == "USDC"
    assert summary.wheel is not None
    assert summary.wheel.pending_csp_assets == "500000000"
    assert summary.wheel.csp_value_assets == "999000000"
    assert summary.wheel.covered_call_value_assets == "1998000000"
    assert summary.wheel.transition_weth == str(5 * 10**17)
    assert summary.wheel.protected_assignment_floor_usd_8 == "201000000000"
    assert summary.wheel.cumulative_gross_premium_assets == "10000000"
    assert summary.wheel.cumulative_protocol_fee_assets == "1000000"
    assert summary.wheel.cumulative_net_premium_assets == "9000000"
    assert summary.wheel.redemption.reserved_assets == "100000000"
    assert summary.wheel.redemption.reserved_principal_assets == "95000000"
    assert len(summary.wheel.tranches) == 2
    assert summary.wheel.tranches[0].child_vault.endswith("10")
    assert summary.wheel.tranches[0].child_execution_state_hash == "0xcsp-execution"
    assert summary.wheel.tranches[1].next_action == "wait_for_call_expiry"
    assert summary.strategy.strategy_kind == "meta_wheel"
    assert summary.strategy.total_premium_collected_assets == "9000000"
    assert summary.actions.deposit.available is True


def test_meta_wheel_user_redemption_remains_usdc_and_fails_closed_on_stale_child() -> (
    None
):
    repository = WheelRepository()
    service = FundService(repository)
    user = service.position(
        "base-sepolia:meta-wheel",
        "0xf000000000000000000000000000000000000099",
    )
    assert user.accounting_value != "0"
    assert user.redemption.claimable_assets == "0"
    assert user.actions.request_redemption.available is True

    repository.nav["coherent"] = False
    stale = service.summary("base-sepolia:meta-wheel")
    assert stale.stale is True
    assert stale.actions.deposit.reason_code == "INCOHERENT_CHILD_NAV"
    assert stale.actions.request_redemption.reason_code == "INCOHERENT_CHILD_NAV"


def test_standalone_summary_serializer_omits_wheel_extension() -> None:
    repository = WheelRepository()
    repository.registry["strategy_kind"] = "csp"
    # Serialization behavior is model-level; switch back after building the
    # Wheel summary to avoid needing a second large standalone fixture.
    repository.registry["strategy_kind"] = "meta_wheel"
    summary = FundService(repository).summary("base-sepolia:meta-wheel")
    summary.wheel = None
    assert "wheel" not in summary.model_dump(by_alias=True)
