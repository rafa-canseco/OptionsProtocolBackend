from dataclasses import dataclass
from typing import Any

from src.db.database import get_client
from src.fund_indexer.projector import FundProjection


@dataclass(frozen=True, slots=True)
class PositionLedger:
    adapter_address: str
    position_id: int
    protocol_vault_id: int
    controller_short_amount: int
    settler_custody_amount: int
    physical_delivery_reserved: int


@dataclass(frozen=True, slots=True)
class OnchainFundSnapshot:
    chain_id: int
    fund_address: str
    block_number: int
    block_hash: str
    share_supply: int
    reserved_claim_assets: int
    flow_reserved_assets: int
    claim_escrow_balance: int
    adapter_usdc: int
    adapter_weth: int
    position_ledgers: tuple[PositionLedger, ...]
    nav_positions_hash: str = "0x" + "00" * 32
    strategy_positions_hash: str = "0x" + "00" * 32
    adapter_nonces: tuple[tuple[str, int], ...] = ()
    reporter_set_version: int = 0
    reporter_threshold: int = 0
    active_reporter_count: int = 0
    active_reporters: tuple[str, ...] = ()
    fee_recipient: str = "0x0000000000000000000000000000000000000000"
    management_fee_wad: int = 0
    performance_fee_bps: int = 0
    high_water_mark: int = 0
    last_report_nonce: int = 0


def reconcile(
    projection: FundProjection,
    snapshot: OnchainFundSnapshot,
) -> dict[str, Any]:
    if projection.fund["chain_id"] != snapshot.chain_id:
        raise ValueError("Projection and reconciliation snapshot chain differ")
    if projection.fund["fund_address"] != snapshot.fund_address.lower():
        raise ValueError("Projection and reconciliation snapshot fund differ")

    accounting_asset = projection.fund.get("accounting_asset")
    weth = projection.fund.get("weth")
    projected_usdc = projection.inventory.get(
        (accounting_asset, "strategy_accounted"), 0
    )
    projected_weth = projection.inventory.get((weth, "assigned"), 0)
    checks = {
        "share_supply": _check(
            int(projection.fund["share_supply"]), snapshot.share_supply
        ),
        "fund_reserved_claim_assets": _check(
            int(projection.fund["reserved_claim_assets"]),
            snapshot.reserved_claim_assets,
        ),
        "flow_reserved_assets": _check(
            snapshot.reserved_claim_assets, snapshot.flow_reserved_assets
        ),
        "claim_escrow_solvency": {
            "expected": str(snapshot.reserved_claim_assets),
            "actual": str(snapshot.claim_escrow_balance),
            "passed": snapshot.claim_escrow_balance >= snapshot.reserved_claim_assets,
        },
        "adapter_usdc": _check(projected_usdc, snapshot.adapter_usdc),
        "adapter_weth": _check(projected_weth, snapshot.adapter_weth),
        "positions_hash": {
            "expected": snapshot.nav_positions_hash,
            "actual": snapshot.strategy_positions_hash,
            "passed": snapshot.nav_positions_hash == snapshot.strategy_positions_hash,
        },
        "adapter_nonces": _reconcile_adapter_nonces(projection, snapshot),
        "active_reporter_count": _check(
            len(projection.fund.get("active_reporters", [])),
            snapshot.active_reporter_count,
        ),
        "active_reporters": _check_exact(
            projection.fund.get("active_reporters", []), list(snapshot.active_reporters)
        ),
        "last_report_nonce": _check(
            int(projection.fund.get("last_report_nonce", 0)),
            snapshot.last_report_nonce,
        ),
        "v1_ledgers": _reconcile_positions(projection, snapshot.position_ledgers),
    }
    return {
        "chain_id": snapshot.chain_id,
        "fund_address": snapshot.fund_address.lower(),
        "block_number": snapshot.block_number,
        "block_hash": snapshot.block_hash,
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
    }


def _check(expected: int, actual: int) -> dict[str, str | bool]:
    return {
        "expected": str(expected),
        "actual": str(actual),
        "passed": expected == actual,
    }


def _check_exact(expected: Any, actual: Any) -> dict[str, Any]:
    return {"expected": expected, "actual": actual, "passed": expected == actual}


def _reconcile_positions(
    projection: FundProjection,
    ledgers: tuple[PositionLedger, ...],
) -> dict[str, Any]:
    failures = []
    by_id = {(ledger.adapter_address, ledger.position_id): ledger for ledger in ledgers}
    for (adapter, position_id), position in projection.positions.items():
        if position["lifecycle"] not in {"open", "awaiting_physical_delivery"}:
            continue
        ledger = by_id.get((adapter, position_id))
        option_amount = int(position["option_amount"])
        expected_reserved = option_amount if position["lifecycle"] == "open" else 0
        if ledger is None:
            failures.append(
                {
                    "adapter_address": adapter,
                    "position_id": position_id,
                    "reason": "missing",
                }
            )
            continue
        if ledger.protocol_vault_id != position["protocol_vault_id"]:
            failures.append(_position_failure(adapter, position_id, "vault_id"))
        if ledger.controller_short_amount != option_amount:
            failures.append(_position_failure(adapter, position_id, "controller"))
        expected_custody = (
            {option_amount} if position["lifecycle"] == "open" else {0, option_amount}
        )
        if ledger.settler_custody_amount not in expected_custody:
            failures.append(_position_failure(adapter, position_id, "custody"))
        if ledger.physical_delivery_reserved != expected_reserved:
            failures.append(_position_failure(adapter, position_id, "reservation"))
    return {"passed": not failures, "failures": failures}


def _position_failure(adapter: str, position_id: int, reason: str) -> dict[str, Any]:
    return {
        "adapter_address": adapter,
        "position_id": position_id,
        "reason": reason,
    }


def _reconcile_adapter_nonces(
    projection: FundProjection, snapshot: OnchainFundSnapshot
) -> dict[str, Any]:
    actual = dict(snapshot.adapter_nonces)
    failures = [
        {"adapter_address": adapter, "expected": nonce, "actual": actual.get(adapter)}
        for adapter, nonce in sorted(projection.adapter_nonces.items())
        if actual.get(adapter) != nonce
    ]
    return {"passed": not failures, "failures": failures}


def store_reconciliation(row: dict[str, Any]) -> None:
    get_client().table("v2_fund_reconciliations").upsert(
        row,
        on_conflict="chain_id,fund_address,block_number",
    ).execute()
