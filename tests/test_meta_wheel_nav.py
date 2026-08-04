from dataclasses import replace

import pytest
from eth_abi import decode

from src.meta_wheel.nav import (
    ChildNavReport,
    WheelNavInput,
    compose_wheel_nav,
    encode_lane_valuations,
)
from src.meta_wheel.store import nav_snapshot_row


BLOCK_HASH = "0x" + "12" * 32
STATE_HASH = "0x" + "34" * 32
CSP = "0x0000000000000000000000000000000000000010"
CALL = "0x0000000000000000000000000000000000000020"


def nav_input() -> WheelNavInput:
    return WheelNavInput(
        snapshot_block=100,
        snapshot_block_hash=BLOCK_HASH,
        expected_active_child_vaults=(CSP, CALL),
        child_reports=(
            ChildNavReport(
                child_vault=CSP,
                strategy_kind="csp",
                custody_domain="0xcsp",
                snapshot_block=100,
                snapshot_block_hash=BLOCK_HASH,
                valid_after_block=95,
                valid_until_block=105,
                child_shares=1_000_000,
                position_state_hash=STATE_HASH,
                expected_position_state_hash=STATE_HASH,
                gross_assets_usdc=1_100_000_000,
                liabilities_usdc=100_000_000,
                liquid_usdc=100_000_000,
                base_exit_cost_usdc=1_000_000,
                data_hash="0x" + "45" * 32,
                valuation_data="0x",
            ),
            ChildNavReport(
                child_vault=CALL,
                strategy_kind="covered_call",
                custody_domain="0xcc",
                snapshot_block=100,
                snapshot_block_hash=BLOCK_HASH,
                valid_after_block=95,
                valid_until_block=105,
                child_shares=10**18,
                position_state_hash=STATE_HASH,
                expected_position_state_hash=STATE_HASH,
                gross_assets_usdc=2_200_000_000,
                liabilities_usdc=200_000_000,
                liquid_usdc=50_000_000,
                base_exit_cost_usdc=2_000_000,
                data_hash="0x" + "67" * 32,
                valuation_data="0x",
            ),
        ),
        parent_idle_usdc=200_000_000,
        pending_csp_usdc=500_000_000,
        redemption_reserved_usdc=100_000_000,
        transition_weth=5 * 10**17,
        actual_coordinator_usdc=600_000_000,
        actual_coordinator_weth=5 * 10**17,
        weth_spot_price_8=2_000 * 10**8,
        stress_weth_price_8=1_500 * 10**8,
        parent_exit_cost_usdc=10_000_000,
    )


def test_wheel_nav_counts_each_custody_domain_once_in_usdc() -> None:
    value = nav_input()
    result = compose_wheel_nav(value)

    assert result.child_csp_value_assets == 999_000_000
    assert result.child_covered_call_value_assets == 1_998_000_000
    assert result.transition_weth_value_assets == 1_000_000_000
    assert result.gross_assets == 5_100_000_000
    assert result.liabilities == 413_000_000
    assert result.net_assets == 4_687_000_000
    assert result.stress_net_assets == 4_437_000_000

    row = nav_snapshot_row(
        chain_id=84532,
        fund_address="0xF000000000000000000000000000000000000001",
        report_nonce=7,
        inputs=value,
        result=result,
        observed_at="2099-07-31T00:00:00Z",
    )
    assert row["net_assets"] == "4687000000"
    assert row["fund_address"] == "0xf000000000000000000000000000000000000001"
    assert len(row["child_reports"]) == 2
    encoded = encode_lane_valuations(value.child_reports)
    decoded = decode(["(address,uint64,uint256,bytes32,bytes)[]"], encoded)[0]
    assert decoded[0][0].lower() == CSP
    assert decoded[0][1:3] == (100, 1_000_000)
    assert decoded[0][4] == b""


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda value: replace(
                value,
                child_reports=(
                    replace(value.child_reports[0], snapshot_block=99),
                    value.child_reports[1],
                ),
            ),
            "different block",
        ),
        (
            lambda value: replace(
                value,
                child_reports=(
                    replace(value.child_reports[0], valid_until_block=99),
                    value.child_reports[1],
                ),
            ),
            "stale",
        ),
        (
            lambda value: replace(
                value,
                child_reports=(
                    replace(
                        value.child_reports[0],
                        expected_position_state_hash="0x" + "56" * 32,
                    ),
                    value.child_reports[1],
                ),
            ),
            "position-state hash",
        ),
        (
            lambda value: replace(value, actual_coordinator_usdc=1),
            "USDC balance",
        ),
        (
            lambda value: replace(value, expected_active_child_vaults=(CSP,)),
            "does not match active lanes",
        ),
    ],
)
def test_wheel_nav_fails_closed_on_incoherent_inputs(change, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        compose_wheel_nav(change(nav_input()))


def test_assignment_basis_cannot_enter_wheel_nav() -> None:
    assert "assignment" not in WheelNavInput.__dataclass_fields__
