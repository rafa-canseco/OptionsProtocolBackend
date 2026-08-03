"""Persistence boundary for coherent Meta Wheel NAV snapshots."""

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from supabase import Client

from src.db.database import get_client
from src.meta_wheel.nav import WheelNavInput, WheelNavResult


def nav_snapshot_row(
    *,
    chain_id: int,
    fund_address: str,
    report_nonce: int,
    inputs: WheelNavInput,
    result: WheelNavResult,
    observed_at: str | None = None,
) -> dict[str, Any]:
    if report_nonce <= 0:
        raise ValueError("Meta Wheel NAV report nonce must be positive")
    if (
        result.snapshot_block != inputs.snapshot_block
        or result.snapshot_block_hash.lower() != inputs.snapshot_block_hash.lower()
        or not result.coherent
    ):
        raise ValueError("Meta Wheel NAV result is not bound to its inputs")
    return {
        "chain_id": chain_id,
        "fund_address": fund_address.lower(),
        "report_nonce": report_nonce,
        "snapshot_block": result.snapshot_block,
        "snapshot_block_hash": result.snapshot_block_hash,
        "coherent": True,
        "gross_assets": str(result.gross_assets),
        "liabilities": str(result.liabilities),
        "net_assets": str(result.net_assets),
        "parent_idle_usdc": str(inputs.parent_idle_usdc),
        "pending_csp_usdc": str(inputs.pending_csp_usdc),
        "redemption_reserved_usdc": str(inputs.redemption_reserved_usdc),
        "transition_weth": str(inputs.transition_weth),
        "transition_weth_value_assets": str(
            result.transition_weth_value_assets
        ),
        "child_csp_value_assets": str(result.child_csp_value_assets),
        "child_covered_call_value_assets": str(
            result.child_covered_call_value_assets
        ),
        "parent_exit_cost_usdc": str(inputs.parent_exit_cost_usdc),
        "weth_spot_price_8": str(inputs.weth_spot_price_8),
        "stress_net_assets": (
            str(result.stress_net_assets)
            if result.stress_net_assets is not None
            else None
        ),
        "child_reports": [asdict(report) for report in inputs.child_reports],
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
    }


def store_nav_snapshot(row: dict[str, Any], client: Client | None = None) -> None:
    (client or get_client()).rpc(
        "v2_upsert_meta_wheel_nav_snapshot",
        {"p_snapshot": row},
    ).execute()
