"""Meta Wheel portion of the one-call atomic snapshot plan."""

from __future__ import annotations

from typing import Any

from eth_abi import encode
from eth_account.messages import encode_typed_data
from hexbytes import HexBytes
from web3 import Web3

from src.config import settings
from src.fund_indexer.mm_snapshot import (
    NAV,
    STRATEGY_CONFIG,
    PlannedCall,
    PlanResult,
    _call,
    _common_calls,
    _quote_tuple,
    decode_common,
    _roles,
    _series,
    _series_calls,
)
from src.pricing.black_scholes import OptionType, delta

SUMMARY = "(uint64,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"
TRANCHE = "(uint8,address,uint64,uint64,uint256,uint256,uint256,uint256,uint256,bytes32,bytes32)"
LOT = "(address,uint64,uint8,uint256,uint256,uint256,uint256,uint256)"
FEE = "(uint64,uint16,uint16,uint16,uint32,uint32,address)"
COMPONENT = "(address,uint64,uint64,bytes32,bool)"
QUOTE_TUPLE = "(address,uint256,uint256,uint256,uint256,uint256)"
LANE_FIELDS = (
    ("coordinator", "coordinator()", "address"),
    ("adapter", "adapter()", "address"),
    ("kind", "laneKind()", "uint8"),
    ("state", "laneState()", "uint8"),
    ("nonce", "stateNonce()", "uint64"),
    ("shares", "childShares()", "uint256"),
    ("tranche_id", "activeTrancheId()", "uint256"),
    ("position_id", "activePositionId()", "uint256"),
    ("max_assets", "maxAssets()", "uint256"),
    ("execution_hash", "executionStateHash()", "bytes32"),
    ("position_hash", "positionStateHash()", "bytes32"),
    ("lot_id", "consumedLotId()", "uint256"),
)


def plain_quote_hash(row: dict[str, Any], chain_id: int, settler: str) -> bytes:
    signable = encode_typed_data(
        domain_data={
            "name": "b1nary",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(settler),
        },
        message_types={
            "Quote": [
                {"name": "oToken", "type": "address"},
                {"name": "bidPrice", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "quoteId", "type": "uint256"},
                {"name": "maxAmount", "type": "uint256"},
                {"name": "makerNonce", "type": "uint256"},
            ]
        },
        message_data={
            "oToken": Web3.to_checksum_address(row["otoken_address"]),
            "bidPrice": int(row["bid_price"]),
            "deadline": int(row["deadline"]),
            "quoteId": int(row["quote_id"]),
            "maxAmount": int(row["max_amount"]),
            "makerNonce": int(row["maker_nonce"]),
        },
    )
    return bytes(
        Web3.keccak(b"\x19" + signable.version + signable.header + signable.body)
    )


def build_meta_plan(
    fund, *, include_common: bool
) -> tuple[list[PlannedCall], dict[str, Any]]:
    roles = _roles(fund)
    coordinator = roles["wheel_coordinator"]
    vault = roles["fund_vault"]
    strategy = roles["strategy_manager"]
    accounting = roles["fund_accounting"]
    flow = roles["fund_flow_manager"]
    settler = roles["batch_settler"]
    oracle = roles["oracle"]
    calls = [
        _call("summary", coordinator, "summary()", SUMMARY),
        _call("registered_count", coordinator, "registeredLaneCount()", "uint256"),
        _call("coordinator_hash", coordinator, "positionStateHash()", "bytes32"),
        _call("policy_hash", coordinator, "policyHash()", "bytes32"),
        _call("floor_buffer", coordinator, "floorBufferUsd8()", "uint256"),
        _call("lane_caps", coordinator, "laneCaps()", ("uint16", "uint16")),
        _call("nav", vault, "activeNavWindow()", NAV),
        _call("strategy_hash", strategy, "positionsHash()", "bytes32"),
        _call(
            "position_nonce",
            strategy,
            "positionNonce(address)",
            "uint64",
            ("address",),
            (Web3.to_checksum_address(coordinator),),
        ),
        _call(
            "strategy_config",
            strategy,
            "strategyConfig(address)",
            STRATEGY_CONFIG,
            ("address",),
            (Web3.to_checksum_address(coordinator),),
        ),
        _call("fee", accounting, "feeConfig()", FEE),
        _call("protocol_fee", settler, "protocolFeeBps()", "uint256"),
        _call("treasury", settler, "treasury()", "address"),
        _call("pending_shares", flow, "totalPendingShares()", "uint256"),
        _call("processing", flow, "hasActiveProcessing()", "bool"),
        _call(
            "spot",
            oracle,
            "getPrice(address)",
            "uint256",
            ("address",),
            (Web3.to_checksum_address(fund.registry.weth),),
        ),
        _call("total_assets", vault, "totalAssets()", "uint256"),
        _call("idle_assets", vault, "accountedIdleAssets()", "uint256"),
        _call("fund_flow_nonce", vault, "fundFlowNonce()", "uint64"),
        _call(
            "coordinator_usdc",
            fund.registry.accounting_asset,
            "balanceOf(address)",
            "uint256",
            ("address",),
            (Web3.to_checksum_address(coordinator),),
        ),
        _call(
            "coordinator_weth",
            fund.registry.weth,
            "balanceOf(address)",
            "uint256",
            ("address",),
            (Web3.to_checksum_address(coordinator),),
        ),
    ]
    component_id = Web3.solidity_keccak(
        ["string", "address"], ["STRATEGY", coordinator]
    )
    calls.append(
        _call(
            "component",
            accounting,
            "componentState(bytes32)",
            COMPONENT,
            ("bytes32",),
            (bytes(component_id),),
        )
    )
    tranches = fund.inputs.get("meta_tranches") or []
    lots = fund.inputs.get("meta_lots") or []
    lanes = fund.inputs.get("meta_lanes") or []
    for row in tranches:
        tranche_id = int(row["tranche_id"])
        calls.append(
            _call(
                f"tranche.{tranche_id}",
                coordinator,
                "tranche(uint256)",
                TRANCHE,
                ("uint256",),
                (tranche_id,),
            )
        )
    for row in lots:
        lot_id = int(row["lot_id"])
        calls.append(
            _call(
                f"lot.{lot_id}",
                coordinator,
                "assignmentLot(uint256)",
                LOT,
                ("uint256",),
                (lot_id,),
            )
        )
    for index, row in enumerate(lanes):
        address = str(row["child_vault"]).lower()
        calls.append(
            _call(
                f"registered.{index}",
                coordinator,
                "registeredLaneAt(uint256)",
                ("address", "uint8", "bool"),
                ("uint256",),
                (index,),
            )
        )
        for field, signature, output in LANE_FIELDS:
            calls.append(_call(f"lane.{address}.{field}", address, signature, output))
        accounting_output = (
            ("uint256", "uint256")
            if row["lane_type"] == "csp"
            else ("uint256", "uint256", "uint256", "uint256")
        )
        calls.append(
            _call(
                f"lane.{address}.accounting",
                address,
                "accountingState()",
                accounting_output,
            )
        )
        calls.extend(
            [
                _call(
                    f"lane.{address}.raw_usdc",
                    fund.registry.accounting_asset,
                    "balanceOf(address)",
                    "uint256",
                    ("address",),
                    (Web3.to_checksum_address(address),),
                ),
                _call(
                    f"lane.{address}.raw_weth",
                    fund.registry.weth,
                    "balanceOf(address)",
                    "uint256",
                    ("address",),
                    (Web3.to_checksum_address(address),),
                ),
            ]
        )
    shared = fund.inputs["common"]
    mm = Web3.to_checksum_address(shared["mm_address"])
    calls.extend(
        [
            _call(
                "mm_nonce",
                settler,
                "makerNonce(address)",
                "uint256",
                ("address",),
                (mm,),
            ),
            _call(
                "mm_whitelisted",
                settler,
                "whitelistedMMs(address)",
                "bool",
                ("address",),
                (mm,),
            ),
        ]
    )
    quotes = fund.inputs.get("quotes") or []
    calls.extend(_series_calls(quotes))
    for row in quotes:
        key = f"{int(row['maker_nonce'])}:{int(row['quote_id'])}:{str(row['otoken_address']).lower()}"
        digest = plain_quote_hash(row, fund.registry.chain_id, settler)
        calls.extend(
            [
                _call(
                    f"quote_hash.{key}",
                    settler,
                    f"hashQuote({QUOTE_TUPLE})",
                    "bytes32",
                    (QUOTE_TUPLE,),
                    (_quote_tuple(row),),
                ),
                _call(
                    f"quote_state.{key}",
                    settler,
                    "getQuoteState(address,bytes32)",
                    ("uint256", "bool"),
                    ("address", "bytes32"),
                    (mm, digest),
                ),
            ]
        )
    if include_common:
        calls.extend(_common_calls(fund, roles))
    return calls, {
        "roles": roles,
        "tranches": tranches,
        "lots": lots,
        "lanes": lanes,
        "quotes": quotes,
        "component_id": Web3.to_hex(component_id),
    }


def _lane_phase(
    kind: str, state: int, tranche: list[Any] | None, active: bool, paused: bool
) -> str:
    leg = int(tranche[0]) if tranche else 0
    if kind == "csp" and leg == 2 and state == 1:
        return "csp_open"
    if kind == "csp" and leg == 3 and state == 2:
        return "csp_settling"
    if kind == "csp" and leg == 3 and state == 3:
        return "csp_ready_for_handoff"
    if kind == "covered_call" and leg == 5 and state == 1:
        return "call_open"
    if kind == "covered_call" and leg == 6 and state == 2:
        return "call_settling"
    if kind == "covered_call" and leg == 6 and state == 3:
        return "call_ready_for_handoff"
    if state == 0 and (not active or paused):
        return "paused"
    if state == 0:
        return "idle"
    raise RuntimeError("Meta Wheel lane/tranche lifecycle is inconsistent")


def _premium(filled: int, amount: int, bid: int, fee_bps: int) -> tuple[int, int]:
    before = filled * bid // 10**8
    after = (filled + amount) * bid // 10**8
    gross = after - before
    fee = after * fee_bps // 10_000 - before * fee_bps // 10_000
    return gross, gross - fee


def _open_data(row: dict[str, Any], amount: int, collateral: int) -> str:
    signature = HexBytes(row["signature"])
    payload = encode(
        ["((address,uint256,uint256,uint256,uint256,uint256),bytes,uint256,uint256)"],
        [(_quote_tuple(row), bytes(signature), amount, collateral)],
    )
    return Web3.to_hex(payload)


def _wheel_quotes(
    fund, values: dict[str, Any], snapshot: dict[str, Any], metadata: dict[str, Any]
) -> list[dict[str, Any]]:
    market = fund.inputs["market"]
    iv = float(market.iv)
    timestamp = int(snapshot["timestamp"])
    spot8 = int(snapshot["spot_price8"])
    fee_bps = int(snapshot["protocol_premium_fee_bps"])
    maker_nonce = int(values["mm_nonce"])
    idle_csp = [lane for lane in snapshot["csp_lanes"] if lane["phase"] == "idle"]
    idle_calls = [lane for lane in snapshot["call_lanes"] if lane["phase"] == "idle"]
    pending = snapshot["pending_csp_tranches"]
    lots = [
        lot
        for lot in snapshot["assignment_lots"]
        if lot["status"] == "available" and int(lot["remaining_weth"]) > 0
    ]
    result = []
    for row in metadata["quotes"]:
        if (
            int(row["maker_nonce"]) != maker_nonce
            or str(row.get("asset", "eth")) != "eth"
        ):
            continue
        token = str(row["otoken_address"]).lower()
        series = _series(values, [row], fund)[token]
        is_put = bool(series["is_put"])
        key = f"{maker_nonce}:{int(row['quote_id'])}:{token}"
        expected = plain_quote_hash(
            row, fund.registry.chain_id, metadata["roles"]["batch_settler"]
        )
        if values[f"quote_hash.{key}"].lower() != Web3.to_hex(expected).lower():
            raise RuntimeError("Meta Wheel quote hash mismatch")
        filled, cancelled = values[f"quote_state.{key}"]
        remaining = 0 if cancelled else max(int(row["max_amount"]) - int(filled), 0)
        if remaining <= 0:
            continue
        strike8 = int(series["strike_price"])
        expiry = int(series["expiry"])
        years = max(expiry - timestamp, 1) / (365 * 86_400)
        delta_bps = round(
            abs(
                delta(
                    OptionType.PUT if is_put else OptionType.CALL,
                    spot8 / 10**8,
                    strike8 / 10**8,
                    years,
                    settings.risk_free_rate,
                    iv,
                )
            )
            * 10_000
        )
        common = {
            "quote_id": f"{maker_nonce}:{int(row['quote_id'])}",
            "is_put": is_put,
            "strike8": strike8,
            "expiry": expiry,
            "created_at": _created_at(row),
            "deadline": int(row["deadline"]),
            "canonical_series": True,
            "delta_bps": delta_bps,
            "execution_slippage_bps": 100,
        }
        if is_put:
            maximum = (remaining * strike8 + 10**10 - 1) // 10**10
            for tranche in pending:
                amount = min(
                    int(tranche["pending_usdc"]) * 10**10 // strike8, remaining
                )
                collateral = (amount * strike8 + 10**10 - 1) // 10**10
                if amount <= 0:
                    continue
                gross, net = _premium(
                    int(filled), amount, int(row["bid_price"]), fee_bps
                )
                for lane in idle_csp:
                    result.append(
                        {
                            **common,
                            "gross_premium_bps": gross * 10_000 // collateral,
                            "maximum_collateral": maximum,
                            "gross_premium": gross,
                            "net_premium": net,
                            "collateral": collateral,
                            "open_data": _open_data(row, amount, collateral),
                            "lane": lane["address"],
                            "tranche_id": int(tranche["tranche_id"]),
                            "lot_id": 0,
                            "allocation_amount": int(tranche["pending_usdc"]),
                        }
                    )
        else:
            maximum = remaining * 10**10
            for lot in lots:
                amount = min(int(lot["remaining_weth"]) // 10**10, remaining)
                collateral = amount * 10**10
                if amount <= 0:
                    continue
                gross, net = _premium(
                    int(filled), amount, int(row["bid_price"]), fee_bps
                )
                for lane in idle_calls:
                    result.append(
                        {
                            **common,
                            "gross_premium_bps": gross * 10_000 // max(collateral, 1),
                            "maximum_collateral": maximum,
                            "gross_premium": gross,
                            "net_premium": net,
                            "collateral": collateral,
                            "open_data": _open_data(row, amount, collateral),
                            "lane": lane["address"],
                            "tranche_id": int(lot["tranche_id"]),
                            "lot_id": int(lot["lot_id"]),
                            "allocation_amount": int(lot["remaining_weth"]),
                        }
                    )
    return result


def _created_at(row: dict[str, Any]) -> int:
    value = row.get("created_at")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        from datetime import datetime

        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    raise RuntimeError("Quote created_at is missing")


def require_meta_reconciled(snapshot: dict[str, Any], *, processing: bool) -> None:
    if not (
        snapshot["nav_coherent"]
        and snapshot["nav_fresh"]
        and snapshot["transition_balances_reconciled"]
        and snapshot["onchain_policy_hash"] == snapshot["nav_policy_hash"]
        and not processing
    ):
        raise RuntimeError("Meta Wheel snapshot reconciliation failed")


def decode_meta(
    fund, result: PlanResult, metadata: dict[str, Any], header, *, include_common: bool
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    values = result.values
    summary = values["summary"]
    if (
        int(summary[1]) != len(metadata["tranches"])
        or int(summary[2]) != len(metadata["lots"])
        or int(values["registered_count"]) != len(metadata["lanes"])
    ):
        raise RuntimeError("Meta Wheel indexed IDs differ from on-chain counts")
    tranches = {
        int(row["tranche_id"]): values[f"tranche.{int(row['tranche_id'])}"]
        for row in metadata["tranches"]
    }
    lots = {
        int(row["lot_id"]): values[f"lot.{int(row['lot_id'])}"]
        for row in metadata["lots"]
    }
    nav = values["nav"]
    nav_row = fund.inputs.get("meta_nav")
    if (
        not nav_row
        or not nav_row.get("coherent")
        or int(nav_row["snapshot_block"]) != int(nav[5])
        or not int(nav[6]) <= header.number <= int(nav[7])
    ):
        raise RuntimeError("Meta Wheel transaction-local NAV observation is incoherent")
    valuations = {
        str(row["child_vault"]).lower(): row
        for row in fund.inputs.get("meta_lane_valuations") or []
    }
    meta_state = fund.inputs.get("meta_state") or {}
    lane_snapshots = {"csp": [], "covered_call": []}
    max_assets = {"csp": set(), "covered_call": set()}
    balances_ok = True
    for index, row in enumerate(metadata["lanes"]):
        address = str(row["child_vault"]).lower()
        registered = values[f"registered.{index}"]
        if str(registered[0]).lower() != address or int(registered[1]) != (
            1 if row["lane_type"] == "csp" else 2
        ):
            raise RuntimeError("Meta Wheel lane registration mismatch")
        tranche_id = int(values[f"lane.{address}.tranche_id"])
        tranche = tranches.get(tranche_id)
        state = int(values[f"lane.{address}.state"])
        accounting = values[f"lane.{address}.accounting"]
        raw_usdc = int(values[f"lane.{address}.raw_usdc"])
        raw_weth = int(values[f"lane.{address}.raw_weth"])
        balances_ok &= int(accounting[0]) <= raw_usdc and int(accounting[1]) <= raw_weth
        valuation = valuations.get(address)
        position_hash = values[f"lane.{address}.position_hash"] if tranche_id else ""
        if tranche_id and (
            not valuation
            or str(valuation["position_state_hash"]).lower()
            != str(position_hash).lower()
        ):
            raise RuntimeError("Meta Wheel lane NAV binding is incomplete")
        lot_id = int(values[f"lane.{address}.lot_id"])
        lane = {
            "address": address,
            "kind": row["lane_type"],
            "phase": _lane_phase(
                row["lane_type"],
                state,
                tranche,
                bool(registered[2]),
                not bool(row["active"]),
            ),
            "tranche_id": tranche_id,
            "transition_nonce": int(values[f"lane.{address}.nonce"]),
            "child_position_id": int(values[f"lane.{address}.position_id"]),
            "amount": int(values[f"lane.{address}.shares"]),
            "expiry": int(tranche[3]) if tranche else 0,
            "execution_state_hash": values[f"lane.{address}.execution_hash"]
            if tranche_id
            else "",
            "tranche_child_execution_state_hash": tranche[9] if tranche else "",
            "position_state_hash": position_hash,
            "nav_position_state_hash": str(valuation["position_state_hash"])
            if valuation
            else "",
            "lot_ids": [lot_id] if lot_id else [],
            "adapter": values[f"lane.{address}.adapter"],
            "dedicated_to_parent": str(values[f"lane.{address}.coordinator"]).lower()
            == metadata["roles"]["wheel_coordinator"].lower(),
            "active_options": int(
                state != 0 and int(values[f"lane.{address}.position_id"]) != 0
            ),
            "tranche_principal_usdc": int(tranche[4]) if tranche else 0,
            "tranche_pending_usdc": int(tranche[5]) if tranche else 0,
            "accounted_usdc": int(accounting[0]),
            "accounted_weth": int(accounting[1]),
            "raw_usdc": raw_usdc,
            "raw_weth": raw_weth,
        }
        lane_snapshots[row["lane_type"]].append(lane)
        max_assets[row["lane_type"]].add(int(values[f"lane.{address}.max_assets"]))
    if any(len(items) != 1 for items in max_assets.values() if items):
        raise RuntimeError("Meta Wheel child lane caps are inconsistent")
    component = values["component"]
    config = values["strategy_config"]
    nav_hash = str(component[3])
    coordinator_hash = str(values["coordinator_hash"])
    nav_coherent = (
        bool(component[4])
        and nav_hash.lower() == coordinator_hash.lower()
        and str(nav[10]).lower() == str(values["strategy_hash"]).lower()
        and int(component[2]) == int(values["position_nonce"])
    )
    fee = values["fee"]
    lots_out = []
    status = {1: "available", 2: "call_open", 3: "called_away", 4: "returned"}
    for lot_id, value in lots.items():
        tranche = tranches[int(value[3])]
        if int(value[2]) not in status:
            raise RuntimeError("Meta Wheel lot status is unsupported")
        lots_out.append(
            {
                "lot_id": lot_id,
                "tranche_id": int(value[3]),
                "tranche_state_nonce": int(tranche[2]),
                "origin_csp_lane": value[0],
                "origin_csp_position_id": int(value[4]),
                "weth_received": int(value[5]),
                "remaining_weth": int(value[6]),
                "literal_assignment_strike8": int(value[7]),
                "created_at": int(value[1]),
                "status": status[int(value[2])],
                "tranche_principal_usdc": int(tranche[4]),
                "tranche_pending_usdc": int(tranche[5]),
            }
        )
    pending = [
        {
            "tranche_id": tranche_id,
            "state_nonce": int(value[2]),
            "pending_usdc": int(value[5]),
            "principal_usdc": int(value[4]),
        }
        for tranche_id, value in tranches.items()
        if int(value[0]) == 1 and int(value[5]) > 0
    ]
    snapshot = {
        "chain_id": fund.registry.chain_id,
        "parent": fund.fund_address,
        "coordinator": metadata["roles"]["wheel_coordinator"],
        "safe_block": header.number,
        "safe_block_confirmations": 2,
        "safe_block_canonical": True,
        "timestamp": int(header.timestamp.timestamp()),
        "onchain_policy_hash": values["policy_hash"],
        "nav_policy_hash": meta_state.get("policy_hash"),
        "onchain_floor_buffer8": int(values["floor_buffer"]),
        "onchain_max_csp_lanes": int(values["lane_caps"][0]),
        "onchain_max_call_lanes": int(values["lane_caps"][1]),
        "onchain_max_usdc_per_csp_lane": next(iter(max_assets["csp"]), 0),
        "onchain_max_weth_per_call_lane": next(iter(max_assets["covered_call"]), 0),
        "coordinator_position_state_hash": coordinator_hash,
        "nav_coordinator_position_state_hash": nav_hash,
        "nav_coherent": nav_coherent,
        "nav_fresh": int(nav[6]) <= header.number <= int(nav[7]),
        "transition_balances_reconciled": int(summary[7])
        <= int(values["coordinator_usdc"])
        and int(summary[8]) == int(summary[6])
        and int(summary[8]) <= int(values["coordinator_weth"])
        and balances_ok
        and not bool(values["processing"]),
        "paused": bool(meta_state.get("paused", False)) or not bool(config[0]),
        "parent_total_assets_usdc": int(values["total_assets"]),
        "idle_usdc": int(values["idle_assets"]),
        "pending_csp_usdc": int(summary[3]),
        "pending_csp_tranches": pending,
        "pending_redemption_usdc": int(meta_state.get("redemption_reserved_usdc", 0)),
        "reserved_redemption_usdc": int(summary[4]),
        "reserved_principal_usdc": int(summary[5]),
        "coordinator_transition_nonce": int(summary[0]),
        "fund_flow_nonce": int(values["fund_flow_nonce"]),
        "spot_price8": int(values["spot"]),
        "protocol_premium_fee_bps": int(values["protocol_fee"]),
        "parent_management_fee_bps": int(fee[0]) * 10_000 // 10**18,
        "parent_performance_fee_bps": int(fee[1]),
        "child_management_fee_bps": 0,
        "child_performance_fee_bps": 0,
        "csp_lanes": lane_snapshots["csp"],
        "call_lanes": lane_snapshots["covered_call"],
        "assignment_lots": lots_out,
        "coordinator_accounted_usdc": int(summary[7]),
        "coordinator_accounted_weth": int(summary[8]),
        "coordinator_transition_weth": int(summary[6]),
        "coordinator_raw_usdc": int(values["coordinator_usdc"]),
        "coordinator_raw_weth": int(values["coordinator_weth"]),
    }
    if not values["mm_whitelisted"]:
        raise RuntimeError("Meta Wheel MM identity is not authorized")
    require_meta_reconciled(snapshot, processing=bool(values["processing"]))
    wheel_quotes = _wheel_quotes(fund, values, snapshot, metadata)
    values["protocol_fee_bps"] = values["protocol_fee"]
    common = decode_common(fund, values) if include_common else None
    return {
        "allocator": {"wheel_snapshot": snapshot, "wheel_quotes": wheel_quotes}
    }, common
