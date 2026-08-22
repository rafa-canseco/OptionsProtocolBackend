import re
from typing import Any

_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_BYTES32 = re.compile(r"0x[0-9a-fA-F]{64}")
_HEX_DATA = re.compile(r"0x(?:[0-9a-fA-F]{2})*")
_QUOTE_ID = re.compile(r"[0-9]+:[0-9]+")
_LANE_PHASES = {
    "idle",
    "csp_open",
    "csp_settling",
    "csp_ready_for_handoff",
    "call_open",
    "call_settling",
    "call_ready_for_handoff",
    "paused",
}
_LOT_STATUSES = {"available", "committed", "call_open", "returned", "called_away"}


def evm_address(value: Any, name: str = "address") -> str:
    if not isinstance(value, str) or _EVM_ADDRESS.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 20-byte EVM address")
    return value


def bytes32(value: Any, name: str = "hash") -> str:
    if not isinstance(value, str) or _BYTES32.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 32-byte hash")
    return value


def _uint(value: Any, name: str, *, positive: bool = False) -> None:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be a {qualifier} integer")


def _int(value: Any, name: str) -> None:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")


def _bool(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _text(value: Any, name: str, allowed: set[str] | None = None) -> None:
    if not isinstance(value, str) or (allowed is not None and value not in allowed):
        raise ValueError(f"{name} must be a valid string")


def _dict(value: Any, name: str, keys: set[str] | None = None) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    if keys is not None and set(value) != keys:
        raise ValueError(f"{name} has an invalid schema")
    return value


def _list(value: Any, name: str, length: int | None = None) -> list[Any]:
    if type(value) is not list or (length is not None and len(value) != length):
        raise ValueError(f"{name} must be a valid list")
    return value


def _uint_list(value: Any, name: str, length: int) -> None:
    for index, item in enumerate(_list(value, name, length)):
        _uint(item, f"{name}[{index}]")


def _nav(value: Any, name: str) -> None:
    items = _list(value, name, 15)
    for index in range(10):
        _uint(items[index], f"{name}[{index}]")
    for index in (10, 11, 12, 14):
        bytes32(items[index], f"{name}[{index}]")
    _uint(items[13], f"{name}[13]")


def _strategy_config(value: Any, name: str) -> None:
    items = _list(value, name, 7)
    _bool(items[0], f"{name}[0]")
    for index in range(1, 5):
        _uint(items[index], f"{name}[{index}]")
    evm_address(items[5], f"{name}[5]")
    _uint(items[6], f"{name}[6]")


def _adapter_config(value: Any, name: str, inner_length: int) -> None:
    items = _list(value, name, 3)
    _uint_list(items[0], f"{name}[0]", inner_length)
    evm_address(items[1], f"{name}[1]")
    _uint(items[2], f"{name}[2]")


def _adapter_state(value: Any, name: str, length: int) -> None:
    items = _list(value, name, length)
    _uint(items[0], f"{name}[0]")
    bytes32(items[1], f"{name}[1]")
    for index in range(2, length):
        _uint(items[index], f"{name}[{index}]")


def _batch(value: Any, name: str) -> None:
    items = _list(value, name, 22)
    for index in range(12):
        _uint(items[index], f"{name}[{index}]")
    bytes32(items[12], f"{name}[12]")
    for index in range(13, 18):
        _uint(items[index], f"{name}[{index}]")
    for index in range(18, 22):
        _bool(items[index], f"{name}[{index}]")


def _operations(value: Any, name: str) -> None:
    keys = {
        "latest_block",
        "batch_id",
        "batch",
        "open_batch_id",
        "nav",
        "eligible_supply",
        "idle_assets",
        "virtual_shares",
        "max_window_outflow_bps",
        "window_eligible_supply",
        "window_processed_shares",
    }
    row = _dict(value, name, keys)
    for key in keys - {"batch", "nav"}:
        _uint(row[key], f"{name}.{key}")
    _batch(row["batch"], f"{name}.batch")
    _nav(row["nav"], f"{name}.nav")


def _series(value: Any, name: str) -> None:
    rows = _dict(value, name)
    keys = {
        "is_put",
        "underlying",
        "strike_asset",
        "collateral_asset",
        "expiry",
        "strike_price",
    }
    for address, raw in rows.items():
        evm_address(address, f"{name} key")
        row = _dict(raw, f"{name}.{address}", keys)
        _bool(row["is_put"], f"{name}.{address}.is_put")
        for key in ("underlying", "strike_asset", "collateral_asset"):
            evm_address(row[key], f"{name}.{address}.{key}")
        _uint(row["expiry"], f"{name}.{address}.expiry")
        _uint(row["strike_price"], f"{name}.{address}.strike_price")


def _quote_states(value: Any, name: str) -> None:
    rows = _dict(value, name)
    for quote_key, raw in rows.items():
        parts = quote_key.split(":", 2)
        if len(parts) != 3 or not parts[0].isdecimal() or not parts[1].isdecimal():
            raise ValueError(f"{name} key must contain nonce:id:address")
        evm_address(parts[2], f"{name} key address")
        row = _dict(raw, f"{name}.{quote_key}", {"filled_amount", "cancelled"})
        _uint(row["filled_amount"], f"{name}.{quote_key}.filled_amount")
        _bool(row["cancelled"], f"{name}.{quote_key}.cancelled")


def _csp_position(value: Any, name: str) -> None:
    items = _list(value, name, 13)
    evm_address(items[0], f"{name}[0]")
    evm_address(items[1], f"{name}[1]")
    for index in range(2, 12):
        _uint(items[index], f"{name}[{index}]")
    bytes32(items[12], f"{name}[12]")


def _covered_call_position(value: Any, name: str) -> None:
    items = _list(value, name, 15)
    evm_address(items[0], f"{name}[0]")
    evm_address(items[1], f"{name}[1]")
    for index in range(2, 14):
        _uint(items[index], f"{name}[{index}]")
    bytes32(items[14], f"{name}[14]")


def _position_expiries(value: Any, name: str) -> None:
    rows = _dict(value, name)
    for position_id, expiry in rows.items():
        if not position_id.isdecimal() or int(position_id) < 1:
            raise ValueError(f"{name} key must be a positive position id")
        _uint(expiry, f"{name}.{position_id}")


def _base_allocator_scalars(row: dict[str, Any], name: str, keys: set[str]) -> None:
    for key in keys:
        _uint(row[key], f"{name}.{key}")
    _bool(row["processing"], f"{name}.processing")


def _csp_state(value: Any) -> None:
    state = _dict(value, "csp state", {"allocator", "operations"})
    keys = {
        "weth",
        "nav",
        "strategy_hash",
        "strategy_config",
        "adapter_config",
        "adapter_state",
        "valuation_policy",
        "total_assets",
        "idle_assets",
        "allocated",
        "minimum_idle_bps",
        "processing",
        "pending_shares",
        "protocol_fee_bps",
        "treasury",
        "series",
        "quote_states",
        "position",
        "position_expiry",
    }
    row = _dict(state["allocator"], "csp allocator", keys)
    evm_address(row["weth"], "csp allocator.weth")
    _nav(row["nav"], "csp allocator.nav")
    bytes32(row["strategy_hash"], "csp allocator.strategy_hash")
    _strategy_config(row["strategy_config"], "csp allocator.strategy_config")
    _adapter_config(row["adapter_config"], "csp allocator.adapter_config", 10)
    _adapter_state(row["adapter_state"], "csp allocator.adapter_state", 6)
    _uint_list(row["valuation_policy"], "csp allocator.valuation_policy", 6)
    _base_allocator_scalars(
        row,
        "csp allocator",
        {
            "total_assets",
            "idle_assets",
            "allocated",
            "minimum_idle_bps",
            "pending_shares",
            "protocol_fee_bps",
            "position_expiry",
        },
    )
    evm_address(row["treasury"], "csp allocator.treasury")
    _series(row["series"], "csp allocator.series")
    _quote_states(row["quote_states"], "csp allocator.quote_states")
    _csp_position(row["position"], "csp allocator.position")
    _operations(state["operations"], "csp operations")


def _covered_call_state(value: Any) -> None:
    state = _dict(value, "covered-call state", {"allocator", "operations"})
    keys = {
        "nav",
        "strategy_hash",
        "strategy_config",
        "adapter_config",
        "adapter_state",
        "positions",
        "valuation_policy",
        "valuation_observers",
        "total_assets",
        "idle_assets",
        "allocated",
        "minimum_idle_bps",
        "processing",
        "pending_shares",
        "spot_price",
        "protocol_fee_bps",
        "series",
        "position_expiries",
    }
    row = _dict(state["allocator"], "covered-call allocator", keys)
    _nav(row["nav"], "covered-call allocator.nav")
    bytes32(row["strategy_hash"], "covered-call allocator.strategy_hash")
    _strategy_config(row["strategy_config"], "covered-call allocator.strategy_config")
    _adapter_config(row["adapter_config"], "covered-call allocator.adapter_config", 11)
    _adapter_state(row["adapter_state"], "covered-call allocator.adapter_state", 7)
    for index, position in enumerate(
        _list(row["positions"], "covered-call allocator.positions")
    ):
        _covered_call_position(position, f"covered-call allocator.positions[{index}]")
    policy = _list(
        row["valuation_policy"], "covered-call allocator.valuation_policy", 10
    )
    for index in range(7):
        _uint(policy[index], f"covered-call allocator.valuation_policy[{index}]")
    evm_address(policy[7], "covered-call allocator.valuation_policy[7]")
    _uint(policy[8], "covered-call allocator.valuation_policy[8]")
    _uint(policy[9], "covered-call allocator.valuation_policy[9]")
    for index, observer in enumerate(
        _list(row["valuation_observers"], "covered-call allocator.valuation_observers")
    ):
        _bool(observer, f"covered-call allocator.valuation_observers[{index}]")
    _base_allocator_scalars(
        row,
        "covered-call allocator",
        {
            "total_assets",
            "idle_assets",
            "allocated",
            "minimum_idle_bps",
            "pending_shares",
            "spot_price",
            "protocol_fee_bps",
        },
    )
    _series(row["series"], "covered-call allocator.series")
    _position_expiries(
        row["position_expiries"], "covered-call allocator.position_expiries"
    )
    _operations(state["operations"], "covered-call operations")


def _pending_tranche(value: Any, name: str) -> None:
    row = _dict(
        value, name, {"tranche_id", "state_nonce", "pending_usdc", "principal_usdc"}
    )
    _uint(row["tranche_id"], f"{name}.tranche_id", positive=True)
    _uint(row["state_nonce"], f"{name}.state_nonce", positive=True)
    _uint(row["pending_usdc"], f"{name}.pending_usdc", positive=True)
    _uint(row["principal_usdc"], f"{name}.principal_usdc")


def _lane(value: Any, name: str, expected_kind: str) -> None:
    keys = {
        "address",
        "kind",
        "phase",
        "tranche_id",
        "transition_nonce",
        "child_position_id",
        "amount",
        "expiry",
        "execution_state_hash",
        "tranche_child_execution_state_hash",
        "position_state_hash",
        "nav_position_state_hash",
        "lot_ids",
        "adapter",
        "dedicated_to_parent",
        "active_options",
        "tranche_principal_usdc",
        "tranche_pending_usdc",
        "accounted_usdc",
        "accounted_weth",
        "raw_usdc",
        "raw_weth",
    }
    row = _dict(value, name, keys)
    evm_address(row["address"], f"{name}.address")
    _text(row["kind"], f"{name}.kind", {expected_kind})
    _text(row["phase"], f"{name}.phase", _LANE_PHASES)
    for key in {
        "tranche_id",
        "transition_nonce",
        "child_position_id",
        "amount",
        "expiry",
        "active_options",
        "tranche_principal_usdc",
        "tranche_pending_usdc",
        "accounted_usdc",
        "accounted_weth",
        "raw_usdc",
        "raw_weth",
    }:
        _uint(row[key], f"{name}.{key}")
    for key in {
        "execution_state_hash",
        "tranche_child_execution_state_hash",
        "position_state_hash",
        "nav_position_state_hash",
    }:
        if row[key] != "":
            bytes32(row[key], f"{name}.{key}")
    for index, lot_id in enumerate(_list(row["lot_ids"], f"{name}.lot_ids")):
        _uint(lot_id, f"{name}.lot_ids[{index}]", positive=True)
    evm_address(row["adapter"], f"{name}.adapter")
    _bool(row["dedicated_to_parent"], f"{name}.dedicated_to_parent")


def _assignment_lot(value: Any, name: str) -> None:
    keys = {
        "lot_id",
        "tranche_id",
        "tranche_state_nonce",
        "origin_csp_lane",
        "origin_csp_position_id",
        "weth_received",
        "remaining_weth",
        "literal_assignment_strike8",
        "created_at",
        "status",
        "tranche_principal_usdc",
        "tranche_pending_usdc",
    }
    row = _dict(value, name, keys)
    for key in {
        "lot_id",
        "tranche_id",
        "tranche_state_nonce",
        "origin_csp_position_id",
        "weth_received",
        "literal_assignment_strike8",
        "created_at",
    }:
        _uint(row[key], f"{name}.{key}", positive=True)
    for key in {"remaining_weth", "tranche_principal_usdc", "tranche_pending_usdc"}:
        _uint(row[key], f"{name}.{key}")
    evm_address(row["origin_csp_lane"], f"{name}.origin_csp_lane")
    _text(row["status"], f"{name}.status", _LOT_STATUSES)


def _wheel_quote(value: Any, name: str) -> None:
    keys = {
        "quote_id",
        "is_put",
        "strike8",
        "expiry",
        "created_at",
        "deadline",
        "gross_premium_bps",
        "maximum_collateral",
        "canonical_series",
        "delta_bps",
        "gross_premium",
        "net_premium",
        "collateral",
        "execution_slippage_bps",
        "open_data",
        "lane",
        "tranche_id",
        "lot_id",
        "allocation_amount",
    }
    row = _dict(value, name, keys)
    if (
        not isinstance(row["quote_id"], str)
        or _QUOTE_ID.fullmatch(row["quote_id"]) is None
    ):
        raise ValueError(f"{name}.quote_id must contain nonce:id")
    _bool(row["is_put"], f"{name}.is_put")
    for key in {
        "strike8",
        "expiry",
        "created_at",
        "deadline",
        "gross_premium_bps",
        "maximum_collateral",
        "gross_premium",
        "net_premium",
        "collateral",
        "execution_slippage_bps",
        "tranche_id",
        "lot_id",
        "allocation_amount",
    }:
        _uint(row[key], f"{name}.{key}")
    _bool(row["canonical_series"], f"{name}.canonical_series")
    _int(row["delta_bps"], f"{name}.delta_bps")
    if (
        not isinstance(row["open_data"], str)
        or _HEX_DATA.fullmatch(row["open_data"]) is None
    ):
        raise ValueError(f"{name}.open_data must be even-length hex")
    evm_address(row["lane"], f"{name}.lane")


def _meta_wheel_state(value: Any) -> None:
    state = _dict(value, "meta-wheel state", {"allocator"})
    allocator = _dict(
        state["allocator"], "meta-wheel allocator", {"wheel_snapshot", "wheel_quotes"}
    )
    keys = {
        "chain_id",
        "parent",
        "coordinator",
        "safe_block",
        "safe_block_confirmations",
        "safe_block_canonical",
        "timestamp",
        "onchain_policy_hash",
        "nav_policy_hash",
        "onchain_floor_buffer8",
        "onchain_max_csp_lanes",
        "onchain_max_call_lanes",
        "onchain_max_usdc_per_csp_lane",
        "onchain_max_weth_per_call_lane",
        "coordinator_position_state_hash",
        "nav_coordinator_position_state_hash",
        "nav_coherent",
        "nav_fresh",
        "transition_balances_reconciled",
        "paused",
        "parent_total_assets_usdc",
        "idle_usdc",
        "pending_csp_usdc",
        "pending_csp_tranches",
        "pending_redemption_usdc",
        "reserved_redemption_usdc",
        "reserved_principal_usdc",
        "coordinator_transition_nonce",
        "fund_flow_nonce",
        "spot_price8",
        "protocol_premium_fee_bps",
        "parent_management_fee_bps",
        "parent_performance_fee_bps",
        "child_management_fee_bps",
        "child_performance_fee_bps",
        "csp_lanes",
        "call_lanes",
        "assignment_lots",
        "coordinator_accounted_usdc",
        "coordinator_accounted_weth",
        "coordinator_transition_weth",
        "coordinator_raw_usdc",
        "coordinator_raw_weth",
    }
    wheel = _dict(allocator["wheel_snapshot"], "wheel snapshot", keys)
    _uint(wheel["chain_id"], "wheel snapshot.chain_id", positive=True)
    evm_address(wheel["parent"], "wheel snapshot.parent")
    evm_address(wheel["coordinator"], "wheel snapshot.coordinator")
    for key in {
        "safe_block",
        "safe_block_confirmations",
        "timestamp",
        "onchain_floor_buffer8",
        "onchain_max_csp_lanes",
        "onchain_max_call_lanes",
        "onchain_max_usdc_per_csp_lane",
        "onchain_max_weth_per_call_lane",
        "parent_total_assets_usdc",
        "idle_usdc",
        "pending_csp_usdc",
        "pending_redemption_usdc",
        "reserved_redemption_usdc",
        "reserved_principal_usdc",
        "coordinator_transition_nonce",
        "fund_flow_nonce",
        "spot_price8",
        "protocol_premium_fee_bps",
        "parent_management_fee_bps",
        "parent_performance_fee_bps",
        "child_management_fee_bps",
        "child_performance_fee_bps",
        "coordinator_accounted_usdc",
        "coordinator_accounted_weth",
        "coordinator_transition_weth",
        "coordinator_raw_usdc",
        "coordinator_raw_weth",
    }:
        _uint(wheel[key], f"wheel snapshot.{key}")
    for key in {
        "safe_block_canonical",
        "nav_coherent",
        "nav_fresh",
        "transition_balances_reconciled",
        "paused",
    }:
        _bool(wheel[key], f"wheel snapshot.{key}")
    for key in {
        "onchain_policy_hash",
        "nav_policy_hash",
        "coordinator_position_state_hash",
        "nav_coordinator_position_state_hash",
    }:
        bytes32(wheel[key], f"wheel snapshot.{key}")
    for index, item in enumerate(
        _list(wheel["pending_csp_tranches"], "wheel snapshot.pending_csp_tranches")
    ):
        _pending_tranche(item, f"wheel snapshot.pending_csp_tranches[{index}]")
    csp_lanes = _list(wheel["csp_lanes"], "wheel snapshot.csp_lanes")
    call_lanes = _list(wheel["call_lanes"], "wheel snapshot.call_lanes")
    if (
        len(csp_lanes) > wheel["onchain_max_csp_lanes"]
        or len(call_lanes) > wheel["onchain_max_call_lanes"]
    ):
        raise ValueError("wheel snapshot lane count exceeds policy")
    for index, item in enumerate(csp_lanes):
        _lane(item, f"wheel snapshot.csp_lanes[{index}]", "csp")
    for index, item in enumerate(call_lanes):
        _lane(item, f"wheel snapshot.call_lanes[{index}]", "covered_call")
    for index, item in enumerate(
        _list(wheel["assignment_lots"], "wheel snapshot.assignment_lots")
    ):
        _assignment_lot(item, f"wheel snapshot.assignment_lots[{index}]")
    for index, item in enumerate(
        _list(allocator["wheel_quotes"], "meta-wheel allocator.wheel_quotes")
    ):
        _wheel_quote(item, f"meta-wheel allocator.wheel_quotes[{index}]")


def validate_snapshot_state(fund_type: str, state: Any) -> None:
    if fund_type == "csp":
        _csp_state(state)
    elif fund_type == "covered_call":
        _covered_call_state(state)
    elif fund_type == "meta_wheel":
        _meta_wheel_state(state)
    else:
        raise ValueError("Unsupported fund type")
