import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from supabase import Client, create_client
from web3 import Web3
from web3._utils.events import get_event_data

from src.config import get_tokenized_fund_rpc_url, settings
from src.contracts.web3_client import create_validated_backend_w3
from src.db.database import get_client
from src.fund_indexer.abis import EVENTS_BY_TOPIC
from src.fund_indexer.models import FundEvent, normalize_address
from src.fund_indexer.projector import project_events
from src.fund_indexer.reconciliation import reconcile
from src.fund_indexer.snapshot import SnapshotContracts, read_onchain_snapshot
from src.meta_wheel.events import WHEEL_EVENTS_BY_TOPIC


logger = logging.getLogger(__name__)
# Keep the persisted name stable so the existing CSP checkpoint is not replayed.
INDEXER_NAME = "tokenized_csp_fund"
DEFAULT_WINDOW = 2_000
MIN_WINDOW = 10
CONFIRMATIONS = 5
SUPPORTED_INTERFACE_VERSIONS = {1}
EVENT_PAGE_SIZE = 1_000
HIGH_FREQUENCY_NAV_EVENTS = ("NavCommitted", "NavSubmitted")
MAX_FAILURE_BACKOFF_SECONDS = 300.0
REGISTRY_REFRESH_SECONDS = 300.0
FUND_REGISTRY_COLUMNS = (
    "chain_id,fund_address,start_block,accounting_asset,weth,strategy_kind,quote_asset"
)
FUND_CONTRACT_COLUMNS = (
    "contract_address,contract_role,interface_version,valid_from_block,"
    "valid_to_block,implementation_address"
)
_monotonic = time.monotonic
EIP1967_IMPLEMENTATION_SLOT = int(
    "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16
)
WHEEL_PREMIUM_TOPIC = next(
    topic
    for topic, abi in WHEEL_EVENTS_BY_TOPIC.items()
    if abi["name"] == "WheelPremiumAccrued"
)
PROXY_ROLES = {
    "fund_vault",
    "fund_share",
    "fund_accounting",
    "fund_flow_manager",
    "strategy_manager",
    "csp_adapter",
    "covered_call_adapter",
    "wheel_coordinator",
    "controller",
    "batch_settler",
}
IMMUTABLE_ROLES = {
    "claim_escrow",
    "access_manager",
    "address_book",
    "csp_valuator",
    "covered_call_valuator",
    "meta_wheel_valuator",
    "margin_pool",
    "nav_verifier",
    "oracle",
    "otoken_factory",
    "swap_router",
    "whitelist",
}


@dataclass(frozen=True, slots=True)
class ContractBinding:
    address: str
    role: str
    interface_version: int
    valid_from_block: int
    valid_to_block: int | None
    implementation_address: str | None = None


@dataclass(frozen=True, slots=True)
class FundRegistry:
    chain_id: int
    fund_address: str
    start_block: int
    accounting_asset: str
    weth: str
    contracts: tuple[ContractBinding, ...]
    strategy_kind: str = "csp"
    quote_asset: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmedHead:
    chain_id: int
    block_number: int
    block_hash: str


def _load_registries(client: Client | None = None) -> list[FundRegistry]:
    client = client or get_client()
    rows = (
        client.table("v2_fund_registry")
        .select(FUND_REGISTRY_COLUMNS)
        .eq("enabled", True)
        .execute()
    )
    return [_registry_from_row(client, row) for row in rows.data or []]


def _load_registry_identity(
    client: Client,
    chain_id: int,
    fund_address: str,
) -> FundRegistry | None:
    result = (
        client.table("v2_fund_registry")
        .select(FUND_REGISTRY_COLUMNS)
        .eq("enabled", True)
        .eq("chain_id", chain_id)
        .eq("fund_address", fund_address)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return _registry_from_row(client, result.data[0])


def _registry_from_row(client: Client, row: dict[str, Any]) -> FundRegistry:
    if int(row["start_block"]) <= 0:
        raise ValueError(f"Fund {row['fund_address']} requires a positive start_block")
    contracts = (
        client.table("v2_fund_contracts")
        .select(FUND_CONTRACT_COLUMNS)
        .eq("chain_id", row["chain_id"])
        .eq("fund_address", row["fund_address"])
        .execute()
    )
    bindings = tuple(
        ContractBinding(
            address=normalize_address(contract["contract_address"]),
            role=contract["contract_role"],
            interface_version=int(contract["interface_version"]),
            valid_from_block=int(contract["valid_from_block"]),
            valid_to_block=(
                int(contract["valid_to_block"])
                if contract["valid_to_block"] is not None
                else None
            ),
            implementation_address=(
                normalize_address(contract["implementation_address"])
                if contract["implementation_address"]
                else None
            ),
        )
        for contract in contracts.data or []
    )
    for binding in bindings:
        _validate_binding(binding)
    return FundRegistry(
        chain_id=int(row["chain_id"]),
        fund_address=normalize_address(row["fund_address"]),
        start_block=int(row["start_block"]),
        accounting_asset=normalize_address(row["accounting_asset"]),
        weth=normalize_address(row["weth"]),
        strategy_kind=row.get("strategy_kind", "csp"),
        quote_asset=(
            normalize_address(row["quote_asset"]) if row.get("quote_asset") else None
        ),
        contracts=bindings,
    )


def _failure_backoff_seconds(interval_seconds: float, failure_count: int) -> float:
    delay = max(0.0, float(interval_seconds))
    for _ in range(max(0, failure_count - 1)):
        delay = min(delay * 2, MAX_FAILURE_BACKOFF_SECONDS)
        if delay == MAX_FAILURE_BACKOFF_SECONDS:
            break
    return min(delay, MAX_FAILURE_BACKOFF_SECONDS)


def _validate_binding(binding: ContractBinding) -> None:
    if binding.role not in PROXY_ROLES | IMMUTABLE_ROLES:
        raise ValueError(f"Unknown contract role {binding.role}")
    if binding.interface_version not in SUPPORTED_INTERFACE_VERSIONS:
        raise ValueError(
            f"Unsupported interface version {binding.interface_version} "
            f"for {binding.address}"
        )
    if binding.role in PROXY_ROLES and binding.implementation_address is None:
        raise ValueError(f"Proxy role {binding.role} requires implementation_address")


def _checkpoint(registry: FundRegistry, client: Client | None = None) -> dict[str, Any]:
    client = client or get_client()
    result = (
        client.table("v2_indexer_checkpoints")
        .select("*")
        .eq("chain_id", registry.chain_id)
        .eq("fund_address", registry.fund_address)
        .eq("indexer_name", INDEXER_NAME)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return {"next_block": registry.start_block, "last_block_hash": None}


def _decode_log(
    w3: Web3,
    registry: FundRegistry,
    bindings: dict[str, list[ContractBinding]],
    log: Any,
) -> FundEvent | None:
    address = normalize_address(log["address"])
    block_number = int(log["blockNumber"])
    binding = next(
        (
            candidate
            for candidate in bindings[address]
            if candidate.valid_from_block <= block_number
            and (
                candidate.valid_to_block is None
                or block_number <= candidate.valid_to_block
            )
        ),
        None,
    )
    if binding is None:
        return None
    topic = Web3.to_hex(log["topics"][0])
    abi = EVENTS_BY_TOPIC.get(topic)
    if abi is None:
        raise ValueError(f"Unknown event topic {topic} from {address}")
    if binding.interface_version not in SUPPORTED_INTERFACE_VERSIONS:
        raise ValueError(
            f"Unsupported interface version {binding.interface_version} for {address}"
        )
    decoded = get_event_data(w3.codec, abi, log)
    args = _json_values(dict(decoded["args"]))
    if decoded["event"] == "Upgraded":
        if binding.role not in PROXY_ROLES or binding.implementation_address is None:
            raise ValueError(f"Unexpected upgrade event from non-proxy {address}")
        implementation = normalize_address(args["implementation"])
        if implementation != binding.implementation_address:
            raise ValueError(
                f"Unregistered implementation {implementation} for proxy {address}"
            )
    return FundEvent(
        chain_id=registry.chain_id,
        fund_address=registry.fund_address,
        contract_address=address,
        contract_role=binding.role,
        interface_version=binding.interface_version,
        block_number=block_number,
        block_hash=Web3.to_hex(decoded["blockHash"]),
        transaction_hash=Web3.to_hex(decoded["transactionHash"]),
        transaction_index=int(decoded["transactionIndex"]),
        log_index=int(decoded["logIndex"]),
        event_name=decoded["event"],
        args=args,
    )


def _json_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_values(item) for item in value]
    if isinstance(value, bytes):
        return Web3.to_hex(value)
    return value


def _load_event_pages(query) -> list[dict[str, Any]]:
    rows = []
    offset = 0
    while True:
        result = query.range(offset, offset + EVENT_PAGE_SIZE - 1).execute()
        page = result.data or []
        if not page:
            break
        rows.extend(page)
        offset += len(page)
    return rows


def _event_query(client: Client, registry: FundRegistry):
    return (
        client.table("v2_chain_events")
        .select("*")
        .eq("chain_id", registry.chain_id)
        .eq("fund_address", registry.fund_address)
    )


def _load_events(
    registry: FundRegistry, client: Client | None = None
) -> list[FundEvent]:
    client = client or get_client()
    state_rows = _load_event_pages(
        _event_query(client, registry)
        .not_.in_("event_name", HIGH_FREQUENCY_NAV_EVENTS)
        .order("block_number")
        .order("transaction_index")
        .order("log_index")
    )
    latest_nav_rows = []
    for event_name in HIGH_FREQUENCY_NAV_EVENTS:
        result = (
            _event_query(client, registry)
            .eq("event_name", event_name)
            .order("block_number", desc=True)
            .order("transaction_index", desc=True)
            .order("log_index", desc=True)
            .limit(1)
            .execute()
        )
        latest_nav_rows.extend(result.data or [])
    rows_by_identity = {
        (
            int(row["block_number"]),
            row["transaction_hash"],
            int(row["log_index"]),
        ): row
        for row in (*state_rows, *latest_nav_rows)
    }
    rows = sorted(
        rows_by_identity.values(),
        key=lambda row: (
            int(row["block_number"]),
            int(row["transaction_index"]),
            int(row["log_index"]),
        ),
    )
    return [
        FundEvent(
            chain_id=int(row["chain_id"]),
            fund_address=row["fund_address"],
            contract_address=row["contract_address"],
            contract_role=row["contract_role"],
            interface_version=int(row["interface_version"]),
            block_number=int(row["block_number"]),
            block_hash=row["block_hash"],
            transaction_hash=row["transaction_hash"],
            transaction_index=int(row["transaction_index"]),
            log_index=int(row["log_index"]),
            event_name=row["event_name"],
            args=row["payload"],
        )
        for row in rows
    ]


def _retain_current_window_history(projection: dict[str, Any], from_block: int) -> None:
    for key in ("nav_reports", "activities"):
        projection[key] = [
            row
            for row in projection.get(key, [])
            if int(row["block_number"]) >= from_block
        ]


def _fetch_window(
    w3: Web3,
    registry: FundRegistry,
    from_block: int,
    to_block: int,
    client: Client | None = None,
) -> list[FundEvent]:
    bindings: dict[str, list[ContractBinding]] = {}
    for contract in registry.contracts:
        bindings.setdefault(contract.address, []).append(contract)
    logs = w3.eth.get_logs(
        {
            "address": [Web3.to_checksum_address(address) for address in bindings],
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [list(EVENTS_BY_TOPIC)],
        }
    )
    events = [
        event
        for log in logs
        if (event := _decode_log(w3, registry, bindings, log)) is not None
    ]
    if registry.strategy_kind == "meta_wheel":
        active_lanes: dict[str, ContractBinding] = {}
        lane_bindings: dict[str, list[ContractBinding]] = {}
        historical = _load_events(registry, client)
        for event in historical:
            if event.contract_role != "wheel_coordinator":
                continue
            if event.event_name == "WheelLaneRegistered":
                lane = normalize_address(event.args["lane"])
                active_lanes[lane] = ContractBinding(
                    address=lane,
                    role="wheel_child_lane",
                    interface_version=event.interface_version,
                    valid_from_block=event.block_number,
                    valid_to_block=None,
                )
            elif event.event_name == "WheelLaneRemoved":
                lane = normalize_address(event.args["lane"])
                active_lanes.pop(lane, None)
        # Scan every lane active at the start of or registered during this
        # window. A lane removed mid-window can still have an earlier premium
        # log in the same window, but it must disappear before the next one.
        lane_bindings.update(
            (lane, [binding]) for lane, binding in active_lanes.items()
        )
        for event in events:
            if event.contract_role != "wheel_coordinator":
                continue
            if event.event_name == "WheelLaneRegistered":
                lane = normalize_address(event.args["lane"])
                binding = ContractBinding(
                    address=lane,
                    role="wheel_child_lane",
                    interface_version=event.interface_version,
                    valid_from_block=event.block_number,
                    valid_to_block=None,
                )
                active_lanes[lane] = binding
                lane_bindings[lane] = [binding]
            elif event.event_name == "WheelLaneRemoved":
                lane = normalize_address(event.args["lane"])
                active_lanes.pop(lane, None)
        if lane_bindings:
            child_logs = w3.eth.get_logs(
                {
                    "address": [
                        Web3.to_checksum_address(address) for address in lane_bindings
                    ],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "topics": [[WHEEL_PREMIUM_TOPIC]],
                }
            )
            events.extend(
                event
                for log in child_logs
                if (event := _decode_log(w3, registry, lane_bindings, log)) is not None
            )
    events = list({event.identity: event for event in events}.values())
    return sorted(
        events,
        key=lambda event: (
            event.block_number,
            event.transaction_index,
            event.log_index,
        ),
    )


def _persist_window(
    w3: Web3,
    registry: FundRegistry,
    from_block: int,
    to_block: int,
    block_hash: str,
    new_events: list[FundEvent],
    client: Client | None = None,
) -> None:
    known = {event.identity: event for event in _load_events(registry, client)}
    known.update({event.identity: event for event in new_events})
    canonical = sorted(
        known.values(),
        key=lambda event: (
            event.block_number,
            event.transaction_index,
            event.log_index,
        ),
    )
    projected = (
        project_events(
            canonical,
            registry.accounting_asset,
            registry.weth,
            strategy_kind=registry.strategy_kind,
            quote_asset=registry.quote_asset,
            to_block=to_block,
        )
        if canonical
        else None
    )
    indexed_at = _block_timestamp(w3, to_block)
    projection = _empty_projection(
        registry,
        to_block,
        block_hash=block_hash,
        indexed_at=indexed_at,
    )
    if projected is not None:
        projection = projected.export()
        missing_roles = _missing_reconciliation_roles(registry, to_block)
        if missing_roles:
            provisional_state = _empty_projection(
                registry,
                to_block,
                block_hash=block_hash,
                indexed_at=indexed_at,
            )["fund_state"][0]
            provisional_state.update(projection["fund_state"][0])
            provisional_state.update(
                as_of_block=to_block,
                as_of_block_hash=block_hash,
                reconciled=False,
                indexed_at=indexed_at,
            )
            projection["fund_state"] = [provisional_state]
            projection["reconciliations"] = []
            logger.info(
                "Deferring fund reconciliation for %s at block %d; "
                "roles activate later: %s",
                registry.fund_address,
                to_block,
                ", ".join(missing_roles),
            )
        else:
            snapshot = read_onchain_snapshot(
                w3,
                projected,
                _snapshot_contracts(registry, to_block),
                to_block,
                block_hash,
            )
            reconciliation = reconcile(projected, snapshot)
            _apply_position_metadata(projected, snapshot, new_events)
            projection = projected.export()
            snapshot_state = dict(
                positions_hash=snapshot.strategy_positions_hash,
                reporter_set_version=snapshot.reporter_set_version,
                reporter_threshold=snapshot.reporter_threshold,
                active_reporter_count=snapshot.active_reporter_count,
                active_reporters=list(snapshot.active_reporters),
                fee_recipient=snapshot.fee_recipient,
                management_fee_wad=str(snapshot.management_fee_wad),
                performance_fee_bps=snapshot.performance_fee_bps,
                high_water_mark=str(snapshot.high_water_mark),
                last_report_nonce=snapshot.last_report_nonce,
                accounted_idle_assets=str(snapshot.accounted_idle_assets),
                virtual_shares=str(snapshot.virtual_shares),
                deposits_paused=snapshot.deposits_paused,
                redemptions_paused=snapshot.redemptions_paused,
                execution_lock_owner=(
                    None
                    if int(snapshot.execution_lock_owner, 16) == 0
                    else snapshot.execution_lock_owner
                ),
                has_active_processing=snapshot.has_active_processing,
                fund_flow_nonce=snapshot.fund_flow_nonce,
                idle_state_hash=snapshot.idle_state_hash,
                normalization_slippage_bps=getattr(
                    snapshot, "normalization_slippage_bps", 0
                ),
                as_of_block=snapshot.block_number,
                as_of_block_hash=snapshot.block_hash,
                reconciled=reconciliation["passed"],
                indexed_at=indexed_at,
            )
            projected.fund.update(snapshot_state)
            projection["fund_state"][0].update(snapshot_state)
            projection["reconciliations"] = [reconciliation]
    _retain_current_window_history(projection, from_block)
    _verify_terminal_hash(w3, to_block, block_hash)
    client = client or get_client()
    client.rpc(
        "v2_ingest_fund_window",
        {
            "p_chain_id": registry.chain_id,
            "p_fund_address": registry.fund_address,
            "p_indexer_name": INDEXER_NAME,
            "p_from_block": from_block,
            "p_to_block": to_block,
            "p_last_block_hash": block_hash,
            "p_events": [event.as_row() for event in new_events],
            "p_projection": projection,
        },
    ).execute()


def _snapshot_contracts(registry: FundRegistry, block_number: int) -> SnapshotContracts:
    by_role = {
        binding.role: binding.address
        for binding in registry.contracts
        if binding.valid_from_block <= block_number
        and (binding.valid_to_block is None or block_number <= binding.valid_to_block)
    }
    adapter_role = {
        "covered_call": "covered_call_adapter",
        "meta_wheel": "wheel_coordinator",
    }.get(registry.strategy_kind, "csp_adapter")
    required = _required_reconciliation_roles(registry.strategy_kind)
    missing = sorted(required - by_role.keys())
    if missing:
        raise ValueError(f"Fund registry is missing reconciliation roles: {missing}")
    return SnapshotContracts(
        fund_vault=by_role["fund_vault"],
        fund_flow_manager=by_role["fund_flow_manager"],
        claim_escrow=by_role["claim_escrow"],
        strategy_adapter=by_role[adapter_role],
        controller=by_role["controller"],
        batch_settler=by_role["batch_settler"],
        strategy_manager=by_role["strategy_manager"],
        fund_accounting=by_role["fund_accounting"],
        strategy_kind=registry.strategy_kind,
    )


def _required_reconciliation_roles(strategy_kind: str) -> set[str]:
    adapter_role = {
        "covered_call": "covered_call_adapter",
        "meta_wheel": "wheel_coordinator",
    }.get(strategy_kind, "csp_adapter")
    return {
        "fund_vault",
        "fund_flow_manager",
        "claim_escrow",
        adapter_role,
        "controller",
        "batch_settler",
        "strategy_manager",
        "fund_accounting",
    }


def _missing_reconciliation_roles(
    registry: FundRegistry, block_number: int
) -> list[str]:
    active_roles = {
        binding.role
        for binding in registry.contracts
        if binding.valid_from_block <= block_number
        and (binding.valid_to_block is None or block_number <= binding.valid_to_block)
    }
    return sorted(_required_reconciliation_roles(registry.strategy_kind) - active_roles)


def _rewind(
    registry: FundRegistry,
    block_number: int,
    client: Client | None = None,
) -> None:
    client = client or get_client()
    client.rpc(
        "v2_rewind_fund_indexer",
        {
            "p_chain_id": registry.chain_id,
            "p_fund_address": registry.fund_address,
            "p_indexer_name": INDEXER_NAME,
            "p_rewind_block": block_number,
        },
    ).execute()


def _empty_projection(
    registry: FundRegistry,
    block_number: int,
    *,
    block_hash: str | None = None,
    indexed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "fund_state": [
            {
                "chain_id": registry.chain_id,
                "fund_address": registry.fund_address,
                "accounting_asset": registry.accounting_asset,
                "weth": registry.weth,
                "strategy_kind": registry.strategy_kind,
                "quote_asset": registry.quote_asset,
                "net_assets": "0",
                "share_supply": "0",
                "reserved_claim_assets": "0",
                "nav_stale": True,
                "positions_hash": None,
                "reporter_set_version": 0,
                "reporter_threshold": 0,
                "active_reporter_count": 0,
                "active_reporters": [],
                "fee_recipient": None,
                "management_fee_wad": "0",
                "performance_fee_bps": 0,
                "high_water_mark": "0",
                "last_report_nonce": 0,
                "accounted_idle_assets": "0",
                "virtual_shares": "0",
                "deposits_paused": True,
                "redemptions_paused": True,
                "execution_lock_owner": None,
                "has_active_processing": False,
                "fund_flow_nonce": 0,
                "idle_state_hash": None,
                "as_of_block": block_number,
                "as_of_block_hash": block_hash,
                "reconciled": False,
                "indexed_at": indexed_at
                or datetime.fromtimestamp(block_number, timezone.utc).isoformat(),
                "nav_valid_after_block": None,
                "nav_valid_until_block": None,
                "last_event_block": block_number,
                "normalization_slippage_bps": 0,
            }
        ],
        "share_balances": [],
        "redemptions": [],
        "positions": [],
        "inventory": [],
        "components": [],
        "nav_reports": [],
        "activities": [],
        "reconciliations": [],
    }


def _apply_position_metadata(
    projection,
    snapshot,
    new_events: list[FundEvent],
) -> None:
    metadata_by_position = {
        (item.adapter_address, item.position_id): item
        for item in getattr(snapshot, "position_metadata", ())
    }
    for key, metadata in metadata_by_position.items():
        position = projection.positions.get(key)
        if position is None:
            continue
        position.update(
            strike_price_8=str(metadata.strike_price_8),
            expiry_timestamp=metadata.expiry_timestamp,
            is_put=metadata.is_put,
        )
    for event in new_events:
        if event.event_name != "PositionOpened":
            continue
        key = normalize_address(event.contract_address), int(event.args["positionId"])
        metadata = metadata_by_position.get(key)
        if metadata is None:
            continue
        event.args.update(
            strikePrice8=str(metadata.strike_price_8),
            expiryTimestamp=metadata.expiry_timestamp,
            isPut=metadata.is_put,
        )


def index_registry_once(
    w3: Web3,
    registry: FundRegistry,
    confirmed_head: ConfirmedHead,
    *,
    client: Client | None = None,
) -> int:
    client = client or get_client()
    if not registry.contracts:
        raise ValueError(f"Fund {registry.fund_address} has no indexed contracts")
    _validate_chain(w3, registry)
    if confirmed_head.chain_id != registry.chain_id:
        raise ValueError(
            "Confirmed head chain mismatch: "
            f"head={confirmed_head.chain_id}, registry={registry.chain_id}"
        )
    checkpoint = _checkpoint(registry, client)
    next_block = int(checkpoint["next_block"])
    if checkpoint.get("last_block_hash") and next_block > registry.start_block:
        parent = w3.eth.get_block(next_block - 1)
        if Web3.to_hex(parent["hash"]) != checkpoint["last_block_hash"]:
            rewind = registry.start_block
            _rewind(registry, rewind, client)
            logger.warning(
                "Reorg detected for %s; rewound to %d", registry.fund_address, rewind
            )
            return 0

    safe_block = confirmed_head.block_number
    if next_block > safe_block:
        return 0
    _validate_proxy_implementations(w3, registry, next_block)
    boundary = _next_binding_boundary(registry, next_block)
    terminal_limit = min(safe_block, boundary - 1 if boundary else safe_block)
    window = min(DEFAULT_WINDOW, terminal_limit - next_block + 1)
    while True:
        to_block = next_block + window - 1
        try:
            block_hash = (
                confirmed_head.block_hash
                if to_block == confirmed_head.block_number
                else Web3.to_hex(w3.eth.get_block(to_block)["hash"])
            )
            events = (
                _fetch_window(w3, registry, next_block, to_block, client)
                if registry.strategy_kind == "meta_wheel"
                else _fetch_window(w3, registry, next_block, to_block)
            )
            break
        except Exception as error:
            if window <= MIN_WINDOW:
                raise
            window = max(MIN_WINDOW, window // 2)
            logger.warning("Reducing fund log window after RPC/decode error: %s", error)
    _validate_proxy_implementations(w3, registry, to_block)
    _persist_window(
        w3,
        registry,
        next_block,
        to_block,
        block_hash,
        events,
        client,
    )
    return len(events)


def _validate_chain(w3: Web3, registry: FundRegistry) -> None:
    observed = int(w3.eth.chain_id)
    if registry.chain_id != settings.chain_id or observed != registry.chain_id:
        raise ValueError(
            "Fund indexer chain mismatch: "
            f"RPC={observed}, registry={registry.chain_id}, settings={settings.chain_id}"
        )


def _active_bindings(registry: FundRegistry, block_number: int):
    return (
        binding
        for binding in registry.contracts
        if binding.valid_from_block <= block_number
        and (binding.valid_to_block is None or block_number <= binding.valid_to_block)
    )


def _next_binding_boundary(registry: FundRegistry, block_number: int) -> int | None:
    boundaries = {
        binding.valid_from_block
        for binding in registry.contracts
        if binding.valid_from_block > block_number
    }
    boundaries.update(
        binding.valid_to_block + 1
        for binding in _active_bindings(registry, block_number)
        if binding.valid_to_block is not None
    )
    return min(boundaries, default=None)


def _validate_proxy_implementations(
    w3: Web3, registry: FundRegistry, block_number: int
) -> None:
    checked_endpoints: set[str] = set()
    checked_implementations: set[str] = set()
    for binding in _active_bindings(registry, block_number):
        _validate_binding(binding)
        if binding.address not in checked_endpoints:
            _require_code(w3, binding.address, binding.role, block_number)
            checked_endpoints.add(binding.address)
        if binding.role not in PROXY_ROLES:
            continue
        implementation = binding.implementation_address
        assert implementation is not None
        if implementation not in checked_implementations:
            _require_code(
                w3, implementation, f"{binding.role} implementation", block_number
            )
            checked_implementations.add(implementation)
        raw = w3.eth.get_storage_at(
            Web3.to_checksum_address(binding.address),
            EIP1967_IMPLEMENTATION_SLOT,
            block_identifier=block_number,
        )
        observed = normalize_address("0x" + bytes(raw)[-20:].hex())
        if observed != binding.implementation_address:
            raise ValueError(
                f"Proxy implementation mismatch for {binding.role} at block "
                f"{block_number}: registry={binding.implementation_address}, RPC={observed}"
            )


def _require_code(w3: Web3, address: str, role: str, block_number: int) -> None:
    code = w3.eth.get_code(
        Web3.to_checksum_address(address), block_identifier=block_number
    )
    if not bytes(code):
        raise ValueError(
            f"Missing bytecode for {role} at {address} at block {block_number}"
        )


def _verify_terminal_hash(w3: Web3, block_number: int, expected: str) -> None:
    observed = Web3.to_hex(w3.eth.get_block(block_number)["hash"])
    if observed != expected:
        raise RuntimeError(
            f"Terminal block {block_number} changed before persistence: "
            f"{expected} != {observed}"
        )


def _block_timestamp(w3: Web3, block_number: int) -> str:
    block = w3.eth.get_block(block_number)
    timestamp = int(block.get("timestamp", block_number))
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _capture_confirmed_head(w3: Web3, chain_id: int) -> ConfirmedHead:
    observed_chain_id = int(w3.eth.chain_id)
    if observed_chain_id != chain_id:
        raise ValueError(
            f"RPC chain mismatch: registry={chain_id}, rpc={observed_chain_id}"
        )
    block_number = int(w3.eth.block_number) - CONFIRMATIONS
    block = w3.eth.get_block(block_number)
    return ConfirmedHead(
        chain_id=chain_id,
        block_number=block_number,
        block_hash=Web3.to_hex(block["hash"]),
    )


def _store_confirmed_head(
    confirmed_head: ConfirmedHead, client: Client | None = None
) -> None:
    client = client or get_client()
    client.rpc(
        "v2_upsert_confirmed_chain_head",
        {
            "p_chain_id": confirmed_head.chain_id,
            "p_block_number": confirmed_head.block_number,
            "p_block_hash": confirmed_head.block_hash,
            "p_observed_at": datetime.now(timezone.utc).isoformat(),
        },
    ).execute()


def _index_cycle(
    w3: Web3,
    registries: list[FundRegistry],
    client: Client | None = None,
) -> None:
    client = client or get_client()
    confirmed_heads: dict[int, ConfirmedHead] = {}
    for registry in registries:
        confirmed_head = confirmed_heads.get(registry.chain_id)
        if confirmed_head is None:
            confirmed_head = _capture_confirmed_head(w3, registry.chain_id)
            _store_confirmed_head(confirmed_head, client)
            confirmed_heads[registry.chain_id] = confirmed_head
        index_registry_once(w3, registry, confirmed_head, client=client)


def _index_registered_funds(w3: Web3) -> None:
    _index_cycle(w3, _load_registries())


def _create_worker_client() -> Client:
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )


def _close_worker_client(client: Client) -> None:
    client.postgrest.aclose()


def _index_registry_once(
    w3: Web3,
    client: Client,
    registry: FundRegistry,
) -> int:
    confirmed_head = _capture_confirmed_head(w3, registry.chain_id)
    _store_confirmed_head(confirmed_head, client)
    return index_registry_once(
        w3,
        registry,
        confirmed_head,
        client=client,
    )


async def _run_registry_worker(
    bootstrap_registry: FundRegistry,
    rpc_url: str,
) -> None:
    chain_id = bootstrap_registry.chain_id
    fund_address = bootstrap_registry.fund_address
    client = _create_worker_client()
    w3 = create_validated_backend_w3(
        rpc_url,
        chain_id,
        "TOKENIZED_FUND_RPC_URL",
    )
    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"fund-index-{fund_address[-6:]}",
    )
    loop = asyncio.get_running_loop()
    registry: FundRegistry | None = bootstrap_registry
    registry_refreshed_at = _monotonic()
    failure_count = 0
    try:
        while True:
            try:
                now = _monotonic()
                if now - registry_refreshed_at >= REGISTRY_REFRESH_SECONDS:
                    registry = await loop.run_in_executor(
                        executor,
                        _load_registry_identity,
                        client,
                        chain_id,
                        fund_address,
                    )
                    registry_refreshed_at = _monotonic()
                if registry is not None:
                    await loop.run_in_executor(
                        executor,
                        _index_registry_once,
                        w3,
                        client,
                        registry,
                    )
                failure_count = 0
                delay = settings.tokenized_fund_indexer_poll_interval_seconds
            except asyncio.CancelledError:
                return
            except Exception:
                failure_count += 1
                delay = _failure_backoff_seconds(
                    settings.tokenized_fund_indexer_poll_interval_seconds,
                    failure_count,
                )
                logger.exception(
                    "Tokenized fund indexing failed for %s:%s",
                    chain_id,
                    fund_address,
                )
            await asyncio.sleep(delay)
    finally:
        shutdown = asyncio.create_task(
            asyncio.to_thread(
                executor.shutdown,
                wait=True,
                cancel_futures=True,
            )
        )
        try:
            await asyncio.shield(shutdown)
        except asyncio.CancelledError:
            await shutdown
        _close_worker_client(client)


async def run() -> None:
    rpc_url = get_tokenized_fund_rpc_url()
    if not rpc_url:
        raise RuntimeError(
            "TOKENIZED_FUND_RPC_URL or RPC_URL is required for the "
            "tokenized fund indexer"
        )
    bootstrap_client = _create_worker_client()
    try:
        registries = await asyncio.to_thread(
            _load_registries,
            bootstrap_client,
        )
    finally:
        _close_worker_client(bootstrap_client)
    if not registries:
        raise RuntimeError("No enabled tokenized funds are registered")

    tasks = []
    for registry in registries:
        tasks.append(
            asyncio.create_task(
                _run_registry_worker(
                    registry,
                    rpc_url,
                )
            )
        )
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        return
    finally:
        for task in tasks:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
