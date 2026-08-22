"""Exact MM recurrent read plans executed through one EIP-1898 Multicall."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from eth_account.messages import encode_typed_data
from hexbytes import HexBytes
from web3 import Web3

from src.config import settings
from src.fund_indexer.snapshot import MULTICALL3_ABI

NAV = "(uint256,uint256,uint256,uint256,uint256,uint64,uint64,uint64,uint64,uint64,bytes32,bytes32,bytes32,uint64,bytes32)"
STRATEGY_CONFIG = "(bool,uint16,uint16,uint32,uint64,address,uint256)"
CSP_ADAPTER_CONFIG = "((uint64,uint64,uint64,uint16,uint16,uint16,uint256,uint256,uint256,uint256),address,uint24)"
CC_ADAPTER_CONFIG = "((uint64,uint64,uint64,uint16,uint16,uint16,uint16,uint256,uint256,uint256,uint256),address,uint24)"
CSP_ADAPTER_STATE = "(uint64,bytes32,uint256,uint256,uint256,uint256)"
CC_ADAPTER_STATE = "(uint64,bytes32,uint256,uint256,uint256,uint256,uint256)"
CSP_POSITION = "(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64,uint8,bytes32)"
CC_POSITION = "(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64,uint8,bytes32)"
BATCH = "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,bytes32,uint64,uint64,uint64,uint16,uint8,bool,bool,bool,bool)"
QUOTE_TUPLE = "(address,uint256,uint256,uint256,uint256,uint256)"

COVERED_CALL_OBSERVERS = (
    "0x3b7f3e42eaCB2E0361aE41e426ea65C6f7896D1e",
    "0x62A7e8c11E4eFc8ed696b2A08D9ccfC339424754",
)


@dataclass(frozen=True, slots=True)
class PlannedCall:
    name: str
    target: str
    signature: str
    output_types: tuple[str, ...]
    input_types: tuple[str, ...] = ()
    arguments: tuple[Any, ...] = ()

    def aggregate_item(self) -> tuple[str, bool, bytes]:
        selector = Web3.keccak(text=self.signature)[:4]
        arguments = encode(list(self.input_types), list(self.arguments))
        return Web3.to_checksum_address(self.target), False, selector + arguments


@dataclass(frozen=True, slots=True)
class PlanResult:
    values: dict[str, Any]
    call_count: int


def execute_plan(w3: Web3, calls: list[PlannedCall], block_hash: str) -> PlanResult:
    if len({call.name for call in calls}) != len(calls):
        raise ValueError("Snapshot call names must be unique")
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(settings.multicall3_address),
        abi=MULTICALL3_ABI,
    )
    data = contract.functions.aggregate3(
        [call.aggregate_item() for call in calls]
    )._encode_transaction_data()
    response = w3.provider.make_request(
        "eth_call",
        [
            {"to": settings.multicall3_address, "data": data},
            {"blockHash": block_hash, "requireCanonical": True},
        ],
    )
    if "error" in response or not response.get("result"):
        raise RuntimeError("MM snapshot Multicall RPC failed")
    raw = w3.codec.decode(["(bool,bytes)[]"], HexBytes(response["result"]))[0]
    if len(raw) != len(calls) or not all(bool(item[0]) for item in raw):
        raise RuntimeError("A required MM snapshot call failed")
    values = {
        call.name: _json_value(
            decode(list(call.output_types), bytes(result[1]))[0]
            if len(call.output_types) == 1
            else decode(list(call.output_types), bytes(result[1]))
        )
        for call, result in zip(calls, raw, strict=True)
    }
    return PlanResult(values, len(calls))


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return Web3.to_hex(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _call(
    name: str,
    target: str,
    signature: str,
    output: str | tuple[str, ...],
    inputs: tuple[str, ...] = (),
    arguments: tuple[Any, ...] = (),
) -> PlannedCall:
    outputs = (output,) if isinstance(output, str) else output
    return PlannedCall(name, target, signature, outputs, inputs, arguments)


def _roles(fund) -> dict[str, str]:
    roles = {
        binding.role: binding.address
        for binding in fund.registry.contracts
        if binding.valid_to_block is None
    }
    return roles


def _quote_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        Web3.to_checksum_address(row["otoken_address"]),
        int(row["bid_price"]),
        int(row["deadline"]),
        int(row["quote_id"]),
        int(row["max_amount"]),
        int(row["maker_nonce"]),
    )


def owner_quote_hash(
    row: dict[str, Any], owner: str, chain_id: int, settler: str
) -> bytes:
    signable = encode_typed_data(
        domain_data={
            "name": "b1nary",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(settler),
        },
        message_types={
            "Quote": [
                {"name": "owner", "type": "address"},
                {"name": "oToken", "type": "address"},
                {"name": "bidPrice", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "quoteId", "type": "uint256"},
                {"name": "maxAmount", "type": "uint256"},
                {"name": "makerNonce", "type": "uint256"},
            ]
        },
        message_data={
            "owner": Web3.to_checksum_address(owner),
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


def _common_calls(fund, roles: dict[str, str]) -> list[PlannedCall]:
    shared = fund.inputs["common"]
    mm = Web3.to_checksum_address(shared["mm_address"])
    usdc = Web3.to_checksum_address(shared["usdc_address"])
    spender = Web3.to_checksum_address(shared["allowance_spender"])
    settler = roles["batch_settler"]
    calls = [
        _call(
            "common.balance", usdc, "balanceOf(address)", "uint256", ("address",), (mm,)
        ),
        _call(
            "common.allowance",
            usdc,
            "allowance(address,address)",
            "uint256",
            ("address", "address"),
            (mm, spender),
        ),
        _call(
            "common.nonce",
            settler,
            "makerNonce(address)",
            "uint256",
            ("address",),
            (mm,),
        ),
        _call(
            "common.spot",
            roles["oracle"],
            "getPrice(address)",
            "uint256",
            ("address",),
            (Web3.to_checksum_address(fund.registry.weth),),
        ),
    ]
    for address, (block_number, _) in sorted(
        (fund.inputs.get("checkpoint_hashes") or {}).items()
    ):
        calls.append(
            _call(
                f"common.checkpoint_hash.{address}",
                settings.multicall3_address,
                "getBlockHash(uint256)",
                "bytes32",
                ("uint256",),
                (block_number,),
            )
        )
    return calls


def _created_at(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        from datetime import datetime

        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    raise RuntimeError("Quote created_at is missing")


def decode_common(fund, values: dict[str, Any]) -> dict[str, Any]:
    configured = fund.inputs["common"]
    for address, (_, expected_hash) in (
        fund.inputs.get("checkpoint_hashes") or {}
    ).items():
        observed = str(values[f"common.checkpoint_hash.{address}"]).lower()
        if observed != expected_hash.lower():
            raise RuntimeError("Fund event checkpoint is not canonical")
    market = fund.inputs["market"]
    quotes = [
        {
            "asset": str(row.get("asset", "eth")),
            "chain": str(row.get("chain", "base")),
            "is_put": bool(row["is_put"]),
            "created_at": _created_at(row["created_at"]),
            "deadline": int(row["deadline"]),
            "expiry": int(row["expiry"]),
            "strike_price": float(row["strike_price"]),
            "deployment_status": str(row["deployment_status"]),
            "otoken_address": str(row["otoken_address"]).lower(),
            "bid_price": int(row["bid_price"]),
            "quote_id": int(row["quote_id"]),
            "max_amount": int(row["max_amount"]),
            "maker_nonce": int(row["maker_nonce"]),
            "signature": str(row["signature"]),
        }
        for row in fund.inputs.get("quotes") or []
    ]
    available = {
        str(row["otoken_address"]).lower(): {
            "address": str(row["otoken_address"]).lower(),
            "strike_price": float(row["strike_price"]),
            "expiry": int(row["expiry"]),
            "is_put": bool(row["is_put"]),
        }
        for row in fund.inputs.get("available_otokens") or []
    }
    if any(row["otoken_address"] not in available for row in quotes):
        raise RuntimeError("Active quote references an unavailable option series")
    return {
        "market": {
            "asset": "eth",
            "spot": int(values["common.spot"]) / 10**8,
            "iv": float(market.iv),
            "iv_source": str(market.iv_source),
            "observed_at": min(
                int(market.observed_at), int(fund.inputs["header_timestamp"])
            ),
            "protocol_fee_bps": int(values["protocol_fee_bps"]),
            "available_otokens": list(available.values()),
        },
        "quotes": quotes,
        "market_maker": {
            **configured,
            "usdc_balance_raw": int(values["common.balance"]),
            "usdc_allowance_raw": int(values["common.allowance"]),
            "maker_nonce": int(values["common.nonce"]),
        },
    }


def _base_fund_calls(
    fund, adapter_role: str, valuator_role: str
) -> tuple[list[PlannedCall], dict[str, str]]:
    roles = _roles(fund)
    vault = roles["fund_vault"]
    flow = roles["fund_flow_manager"]
    strategy = roles["strategy_manager"]
    adapter = roles[adapter_role]
    valuator = roles[valuator_role]
    asset = fund.registry.accounting_asset
    calls = [
        _call("nav", vault, "activeNavWindow()", NAV),
        _call("strategy_hash", strategy, "positionsHash()", "bytes32"),
        _call(
            "strategy_config",
            strategy,
            "strategyConfig(address)",
            STRATEGY_CONFIG,
            ("address",),
            (Web3.to_checksum_address(adapter),),
        ),
        _call("total_assets", vault, "totalAssets()", "uint256"),
        _call("idle_assets", vault, "accountedIdleAssets()", "uint256"),
        _call(
            "allocated",
            strategy,
            "allocatedToAdapter(address,address)",
            "uint256",
            ("address", "address"),
            (Web3.to_checksum_address(adapter), Web3.to_checksum_address(asset)),
        ),
        _call("minimum_idle_bps", strategy, "minimumIdleBps()", "uint16"),
        _call("processing", flow, "hasActiveProcessing()", "bool"),
        _call("pending_shares", flow, "totalPendingShares()", "uint256"),
        _call(
            "protocol_fee_bps", roles["batch_settler"], "protocolFeeBps()", "uint256"
        ),
        _call("next_batch_id", flow, "nextProcessBatchId()", "uint64"),
        _call("open_batch_id", flow, "openBatchId()", "uint64"),
        _call("share_supply", vault, "shareSupply()", "uint256"),
        _call("virtual_shares", vault, "virtualShares()", "uint256"),
        _call("exit_policy", flow, "exitPolicy()", ("uint16", "uint16")),
    ]
    policy_calls = fund.inputs.get("valuator_calls") or []
    for name, signature, output, arguments in policy_calls:
        inputs = tuple(item[0] for item in arguments)
        values = tuple(item[1] for item in arguments)
        calls.append(
            _call(f"valuation.{name}", valuator, signature, output, inputs, values)
        )
    return calls, roles


def _series_calls(quotes: list[dict[str, Any]]) -> list[PlannedCall]:
    calls = []
    for token in sorted(
        {
            str(row["otoken_address"]).lower()
            for row in quotes
            if str(row.get("deployment_status", "ready")).lower() == "ready"
        }
    ):
        for field, signature, output in (
            ("is_put", "isPut()", "bool"),
            ("underlying", "underlying()", "address"),
            ("strike_asset", "strikeAsset()", "address"),
            ("collateral_asset", "collateralAsset()", "address"),
            ("expiry", "expiry()", "uint256"),
            ("strike_price", "strikePrice()", "uint256"),
        ):
            calls.append(_call(f"series.{token}.{field}", token, signature, output))
    return calls


def _operations_dynamic_calls(fund, roles: dict[str, str]) -> list[PlannedCall]:
    prior = (fund.projection.fund.get("snapshot_state") or {}).get("operations") or {}
    indexed_batches = fund.inputs.get("redemption_batches") or []
    indexed_batch = max(
        (int(item["latest_batch_id"]) for item in indexed_batches), default=1
    )
    prior_batch = max(1, int(prior.get("batch_id", indexed_batch)))
    prior_nonce = max(
        0,
        int(
            (
                prior.get("nav")
                or [0] * 9 + [fund.projection.fund.get("last_report_nonce", 0)]
            )[9]
        ),
    )
    flow = roles["fund_flow_manager"]
    calls = []
    for batch_id in sorted({prior_batch, prior_batch + 1}):
        calls.append(
            _call(
                f"batch.{batch_id}",
                flow,
                "batch(uint64)",
                BATCH,
                ("uint64",),
                (batch_id,),
            )
        )
    for nonce in sorted({prior_nonce, prior_nonce + 1}):
        calls.append(
            _call(
                f"outflow.{nonce}",
                flow,
                "windowOutflow(uint64)",
                ("uint256", "uint256"),
                ("uint64",),
                (nonce,),
            )
        )
    return calls


def _series(
    values: dict[str, Any], quotes: list[dict[str, Any]], fund=None
) -> dict[str, Any]:
    result = {}
    by_token = {str(row["otoken_address"]).lower(): row for row in quotes}
    for token, row in sorted(by_token.items()):
        if f"series.{token}.is_put" in values:
            result[token] = {
                key: values[f"series.{token}.{key}"]
                for key in (
                    "is_put",
                    "underlying",
                    "strike_asset",
                    "collateral_asset",
                    "expiry",
                    "strike_price",
                )
            }
            continue
        if fund is None or str(row.get("deployment_status", "")).lower() not in {
            "virtual",
            "creating",
        }:
            raise RuntimeError("Required option series metadata is unavailable")
        result[token] = {
            "is_put": bool(row["is_put"]),
            "underlying": fund.registry.weth,
            "strike_asset": fund.registry.quote_asset or settings.usdc_address.lower(),
            "collateral_asset": (
                fund.registry.accounting_asset
                if not bool(row["is_put"])
                else fund.registry.quote_asset or settings.usdc_address.lower()
            ),
            "expiry": int(row["expiry"]),
            "strike_price": int(float(row["strike_price"]) * 10**8),
        }
    return result


def _operations(values: dict[str, Any]) -> dict[str, Any]:
    nav = values["nav"]
    batch_id = int(values["next_batch_id"])
    nonce = int(nav[9])
    batch_key = f"batch.{batch_id}"
    outflow_key = f"outflow.{nonce}"
    if batch_key not in values or outflow_key not in values:
        raise RuntimeError("Fund operation identity advanced beyond bounded call plan")
    return {
        "latest_block": int(nav[7]),
        "batch_id": batch_id,
        "batch": values[batch_key],
        "open_batch_id": int(values["open_batch_id"]),
        "nav": nav,
        "eligible_supply": int(values["share_supply"]),
        "idle_assets": int(values["idle_assets"]),
        "virtual_shares": int(values["virtual_shares"]),
        "max_window_outflow_bps": int(values["exit_policy"][1]),
        "window_eligible_supply": int(values[outflow_key][0]),
        "window_processed_shares": int(values[outflow_key][1]),
    }


def build_csp_plan(
    fund, *, include_common: bool
) -> tuple[list[PlannedCall], dict[str, Any]]:
    calls, roles = _base_fund_calls(fund, "csp_adapter", "csp_valuator")
    adapter = roles["csp_adapter"]
    calls.extend(
        [
            _call("adapter_config", adapter, "adapterConfig()", CSP_ADAPTER_CONFIG),
            _call("adapter_state", adapter, "adapterState()", CSP_ADAPTER_STATE),
            _call("treasury", roles["batch_settler"], "treasury()", "address"),
        ]
    )
    positions = sorted(fund.projection.positions.items())
    for (_, position_id), position in positions:
        calls.append(
            _call(
                f"position.{position_id}",
                adapter,
                "position(uint256)",
                CSP_POSITION,
                ("uint256",),
                (position_id,),
            )
        )
        calls.append(
            _call(
                f"position_expiry.{position_id}",
                position["otoken_address"],
                "expiry()",
                "uint256",
            )
        )
    quotes = fund.inputs.get("quotes", [])
    calls.extend(_series_calls(quotes))
    mm = fund.inputs["common"]["mm_address"]
    for row in quotes:
        key = f"{int(row['maker_nonce'])}:{int(row['quote_id'])}:{str(row['otoken_address']).lower()}"
        digest = owner_quote_hash(
            row, adapter, fund.registry.chain_id, roles["batch_settler"]
        )
        calls.append(
            _call(
                f"quote_hash.{key}",
                roles["batch_settler"],
                f"hashQuoteFor(address,{QUOTE_TUPLE})",
                "bytes32",
                ("address", QUOTE_TUPLE),
                (Web3.to_checksum_address(adapter), _quote_tuple(row)),
            )
        )
        calls.append(
            _call(
                f"quote_state.{key}",
                roles["batch_settler"],
                "getQuoteState(address,bytes32)",
                ("uint256", "bool"),
                ("address", "bytes32"),
                (Web3.to_checksum_address(mm), digest),
            )
        )
    calls.extend(_operations_dynamic_calls(fund, roles))
    if include_common:
        calls.extend(_common_calls(fund, roles))
    return calls, {"roles": roles, "positions": positions, "quotes": quotes}


def decode_csp(
    fund, result: PlanResult, metadata: dict[str, Any], *, include_common: bool
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    values = result.values
    adapter_state = values["adapter_state"]
    active_id = int(adapter_state[2]) if int(adapter_state[3]) else 0
    position = (
        ["0x0000000000000000000000000000000000000000"] * 2
        + [0] * 10
        + ["0x" + "00" * 32]
    )
    position_expiry = 0
    if active_id:
        if f"position.{active_id}" not in values:
            raise RuntimeError("Active CSP position is absent from durable index")
        position = values[f"position.{active_id}"]
        position_expiry = int(values[f"position_expiry.{active_id}"])
    quote_states = {}
    for row in metadata["quotes"]:
        key = f"{int(row['maker_nonce'])}:{int(row['quote_id'])}:{str(row['otoken_address']).lower()}"
        expected = owner_quote_hash(
            row,
            metadata["roles"]["csp_adapter"],
            fund.registry.chain_id,
            metadata["roles"]["batch_settler"],
        )
        if values[f"quote_hash.{key}"].lower() != Web3.to_hex(expected).lower():
            raise RuntimeError("CSP quote hash differs from canonical local digest")
        filled, cancelled = values[f"quote_state.{key}"]
        quote_states[key] = {"filled_amount": int(filled), "cancelled": bool(cancelled)}
    allocator = {
        "weth": fund.registry.weth,
        "nav": values["nav"],
        "strategy_hash": values["strategy_hash"],
        "strategy_config": values["strategy_config"],
        "adapter_config": values["adapter_config"],
        "adapter_state": adapter_state,
        "valuation_policy": [
            values[f"valuation.{name}"] for name in fund.inputs["valuator_names"]
        ],
        "total_assets": int(values["total_assets"]),
        "idle_assets": int(values["idle_assets"]),
        "allocated": int(values["allocated"]),
        "minimum_idle_bps": int(values["minimum_idle_bps"]),
        "processing": bool(values["processing"]),
        "pending_shares": int(values["pending_shares"]),
        "protocol_fee_bps": int(values["protocol_fee_bps"]),
        "treasury": values["treasury"],
        "series": _series(values, metadata["quotes"], fund),
        "quote_states": quote_states,
        "position": position,
        "position_expiry": position_expiry,
    }
    state = {"allocator": allocator, "operations": _operations(values)}
    common = decode_common(fund, values) if include_common else None
    if values["nav"][10].lower() != values["strategy_hash"].lower():
        raise RuntimeError("CSP NAV and strategy hashes differ")
    return state, common


def build_covered_call_plan(
    fund, *, include_common: bool
) -> tuple[list[PlannedCall], dict[str, Any]]:
    calls, roles = _base_fund_calls(
        fund, "covered_call_adapter", "covered_call_valuator"
    )
    adapter = roles["covered_call_adapter"]
    calls.extend(
        [
            _call("adapter_config", adapter, "adapterConfig()", CC_ADAPTER_CONFIG),
            _call("adapter_state", adapter, "adapterState()", CC_ADAPTER_STATE),
            _call(
                "spot_price",
                roles["oracle"],
                "getPrice(address)",
                "uint256",
                ("address",),
                (Web3.to_checksum_address(fund.registry.weth),),
            ),
        ]
    )
    positions = sorted(fund.projection.positions.items())
    for (_, position_id), position in positions:
        calls.append(
            _call(
                f"position.{position_id}",
                adapter,
                "position(uint256)",
                CC_POSITION,
                ("uint256",),
                (position_id,),
            )
        )
        calls.append(
            _call(
                f"position_expiry.{position_id}",
                position["otoken_address"],
                "expiry()",
                "uint256",
            )
        )
    observers = fund.inputs.get("observers", COVERED_CALL_OBSERVERS)
    for index, observer in enumerate(observers):
        calls.append(
            _call(
                f"observer.{index}",
                roles["covered_call_valuator"],
                "isApprovedObserver(address)",
                "bool",
                ("address",),
                (Web3.to_checksum_address(observer),),
            )
        )
    quotes = fund.inputs.get("quotes", [])
    calls.extend(_series_calls(quotes))
    calls.extend(_operations_dynamic_calls(fund, roles))
    if include_common:
        calls.extend(_common_calls(fund, roles))
    return calls, {
        "roles": roles,
        "positions": positions,
        "quotes": quotes,
        "observers": observers,
    }


def decode_covered_call(
    fund, result: PlanResult, metadata: dict[str, Any], *, include_common: bool
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    values = result.values
    adapter_state = values["adapter_state"]
    count = int(adapter_state[2])
    positions = []
    expiries = {}
    for position_id in range(1, count + 1):
        key = f"position.{position_id}"
        if key not in values:
            raise RuntimeError("Covered-call position count differs from durable index")
        position = values[key]
        positions.append(position)
        expiries[str(position_id)] = int(values[f"position_expiry.{position_id}"])
    allocator = {
        "nav": values["nav"],
        "strategy_hash": values["strategy_hash"],
        "strategy_config": values["strategy_config"],
        "adapter_config": values["adapter_config"],
        "adapter_state": adapter_state,
        "positions": positions,
        "valuation_policy": [
            values[f"valuation.{name}"] for name in fund.inputs["valuator_names"]
        ],
        "valuation_observers": [
            bool(values[f"observer.{index}"])
            for index, _ in enumerate(metadata["observers"])
        ],
        "total_assets": int(values["total_assets"]),
        "idle_assets": int(values["idle_assets"]),
        "allocated": int(values["allocated"]),
        "minimum_idle_bps": int(values["minimum_idle_bps"]),
        "processing": bool(values["processing"]),
        "pending_shares": int(values["pending_shares"]),
        "spot_price": int(values["spot_price"]),
        "protocol_fee_bps": int(values["protocol_fee_bps"]),
        "series": _series(values, metadata["quotes"], fund),
        "position_expiries": expiries,
    }
    state = {"allocator": allocator, "operations": _operations(values)}
    common = decode_common(fund, values) if include_common else None
    if values["nav"][10].lower() != values["strategy_hash"].lower():
        raise RuntimeError("Covered-call NAV and strategy hashes differ")
    return state, common
