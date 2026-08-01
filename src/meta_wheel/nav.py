"""Coherent USDC NAV composition for the Meta Wheel parent.

Child option liabilities and child exit costs are already reflected in each
child's net NAV.  The parent therefore converts and adds child *net* values
exactly once, then subtracts only parent-level claim reserves and exit costs.
Assignment basis intentionally does not appear in this module: it is policy
metadata, never a valuation input.
"""

from dataclasses import dataclass
from typing import Literal

from eth_abi import encode
from web3 import Web3


StrategyKind = Literal["csp", "covered_call"]


@dataclass(frozen=True, slots=True)
class ChildNavReport:
    child_vault: str
    strategy_kind: StrategyKind
    custody_domain: str
    snapshot_block: int
    snapshot_block_hash: str
    valid_after_block: int
    valid_until_block: int
    child_shares: int
    position_state_hash: str
    expected_position_state_hash: str
    gross_assets_usdc: int
    liabilities_usdc: int
    liquid_usdc: int
    base_exit_cost_usdc: int
    data_hash: str
    valuation_data: str


@dataclass(frozen=True, slots=True)
class WheelNavInput:
    snapshot_block: int
    snapshot_block_hash: str
    expected_active_child_vaults: tuple[str, ...]
    child_reports: tuple[ChildNavReport, ...]
    parent_idle_usdc: int
    pending_csp_usdc: int
    redemption_reserved_usdc: int
    transition_weth: int
    actual_coordinator_usdc: int
    actual_coordinator_weth: int
    weth_spot_price_8: int
    parent_exit_cost_usdc: int = 0
    usdc_decimals: int = 6
    weth_decimals: int = 18
    stress_weth_price_8: int | None = None


@dataclass(frozen=True, slots=True)
class WheelNavResult:
    gross_assets: int
    liabilities: int
    net_assets: int
    child_csp_value_assets: int
    child_covered_call_value_assets: int
    transition_weth_value_assets: int
    stress_net_assets: int | None
    snapshot_block: int
    snapshot_block_hash: str
    coherent: bool = True


def compose_wheel_nav(value: WheelNavInput) -> WheelNavResult:
    """Return an authoritative parent NAV or fail closed on incoherence."""

    _validate_non_negative(value)
    expected_lanes = {
        child_vault.lower() for child_vault in value.expected_active_child_vaults
    }
    if len(expected_lanes) != len(value.expected_active_child_vaults):
        raise ValueError("Duplicate active Meta Wheel lane")
    observed_lanes = {report.child_vault.lower() for report in value.child_reports}
    if len(observed_lanes) != len(value.child_reports):
        raise ValueError("Duplicate child NAV report for Meta Wheel lane")
    if observed_lanes != expected_lanes:
        raise ValueError("Meta Wheel child NAV set does not match active lanes")

    custody_domains = [report.custody_domain.lower() for report in value.child_reports]
    if len(custody_domains) != len(set(custody_domains)):
        raise ValueError("Meta Wheel child custody domain counted more than once")

    csp_value = 0
    call_value = 0
    child_deductions = 0
    for report in value.child_reports:
        _validate_child_report(value, report)
        deductions = report.liabilities_usdc + report.base_exit_cost_usdc
        if deductions > report.gross_assets_usdc:
            raise ValueError("Meta Wheel child deductions exceed gross assets")
        net_assets = report.gross_assets_usdc - deductions
        child_deductions += deductions
        if report.strategy_kind == "csp":
            csp_value += net_assets
        else:
            call_value += net_assets

    accounted_coordinator_usdc = (
        value.pending_csp_usdc + value.redemption_reserved_usdc
    )
    if accounted_coordinator_usdc != value.actual_coordinator_usdc:
        raise ValueError("Meta Wheel coordinator USDC balance is not reconciled")
    if value.transition_weth != value.actual_coordinator_weth:
        raise ValueError("Meta Wheel coordinator WETH balance is not reconciled")

    transition_weth_value = _weth_to_usdc(
        value.transition_weth,
        value.weth_spot_price_8,
        value.weth_decimals,
        value.usdc_decimals,
    )
    gross_assets = (
        value.parent_idle_usdc
        + accounted_coordinator_usdc
        + transition_weth_value
        + sum(report.gross_assets_usdc for report in value.child_reports)
    )
    parent_liabilities = (
        value.redemption_reserved_usdc + value.parent_exit_cost_usdc
    )
    liabilities = parent_liabilities + child_deductions
    if liabilities > gross_assets:
        raise ValueError("Meta Wheel parent liabilities exceed gross assets")

    stress_net = None
    if value.stress_weth_price_8 is not None:
        # Child reports are already USDC-denominated and remain unchanged in
        # parent-only spot stress.  Child-specific stress is supplied by the
        # child valuation producer, never approximated a second time here.
        stress_call_value = sum(
            report.gross_assets_usdc
            - report.liabilities_usdc
            - report.base_exit_cost_usdc
            for report in value.child_reports
            if report.strategy_kind == "covered_call"
        )
        stress_transition = _weth_to_usdc(
            value.transition_weth,
            value.stress_weth_price_8,
            value.weth_decimals,
            value.usdc_decimals,
        )
        stress_gross = (
            value.parent_idle_usdc
            + accounted_coordinator_usdc
            + stress_transition
            + csp_value
            + stress_call_value
        )
        stress_net = max(stress_gross - parent_liabilities, 0)

    return WheelNavResult(
        gross_assets=gross_assets,
        liabilities=liabilities,
        net_assets=gross_assets - liabilities,
        child_csp_value_assets=csp_value,
        child_covered_call_value_assets=call_value,
        transition_weth_value_assets=transition_weth_value,
        stress_net_assets=stress_net,
        snapshot_block=value.snapshot_block,
        snapshot_block_hash=value.snapshot_block_hash,
    )


def _validate_child_report(parent: WheelNavInput, child: ChildNavReport) -> None:
    if child.strategy_kind not in {"csp", "covered_call"}:
        raise ValueError("Unsupported Meta Wheel child strategy")
    if child.snapshot_block != parent.snapshot_block:
        raise ValueError("Meta Wheel child NAV is bound to a different block")
    if child.snapshot_block_hash.lower() != parent.snapshot_block_hash.lower():
        raise ValueError("Meta Wheel child NAV is bound to a different block hash")
    if not child.valid_after_block <= parent.snapshot_block <= child.valid_until_block:
        raise ValueError("Meta Wheel child NAV is stale")
    if child.position_state_hash.lower() != child.expected_position_state_hash.lower():
        raise ValueError("Meta Wheel child position-state hash is incoherent")
    if (
        child.child_shares <= 0
        or child.gross_assets_usdc < 0
        or child.liabilities_usdc < 0
        or child.liabilities_usdc > child.gross_assets_usdc
        or child.liquid_usdc < 0
        or child.liquid_usdc > child.gross_assets_usdc
        or child.base_exit_cost_usdc < 0
        or child.liabilities_usdc + child.base_exit_cost_usdc
        > child.gross_assets_usdc
    ):
        raise ValueError("Invalid Meta Wheel child NAV quantity")


def encode_lane_valuations(reports: tuple[ChildNavReport, ...]) -> bytes:
    """Encode exactly the `WheelTypes.LaneValuation[]` valuator boundary."""

    values = [
        (
            Web3.to_checksum_address(report.child_vault),
            report.snapshot_block,
            report.child_shares,
            Web3.to_bytes(hexstr=report.position_state_hash),
            Web3.to_bytes(hexstr=report.valuation_data),
        )
        for report in reports
    ]
    return encode(
        ["(address,uint64,uint256,bytes32,bytes)[]"],
        [values],
    )


def _validate_non_negative(value: WheelNavInput) -> None:
    quantities = (
        value.snapshot_block,
        value.parent_idle_usdc,
        value.pending_csp_usdc,
        value.redemption_reserved_usdc,
        value.transition_weth,
        value.actual_coordinator_usdc,
        value.actual_coordinator_weth,
        value.weth_spot_price_8,
        value.parent_exit_cost_usdc,
    )
    if any(quantity < 0 for quantity in quantities):
        raise ValueError("Meta Wheel NAV quantities cannot be negative")
    if value.weth_spot_price_8 == 0 and (
        value.transition_weth
        or any(report.strategy_kind == "covered_call" for report in value.child_reports)
    ):
        raise ValueError("Meta Wheel WETH exposure requires a positive spot price")
    if value.stress_weth_price_8 is not None and value.stress_weth_price_8 < 0:
        raise ValueError("Meta Wheel stress price cannot be negative")


def _weth_to_usdc(
    weth_amount: int,
    spot_price_8: int,
    weth_decimals: int,
    usdc_decimals: int,
) -> int:
    return (
        weth_amount * spot_price_8 * 10**usdc_decimals
        // 10 ** (weth_decimals + 8)
    )
