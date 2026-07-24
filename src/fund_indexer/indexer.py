import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from web3 import Web3
from web3._utils.events import get_event_data

from src.config import settings
from src.db.database import get_client
from src.fund_indexer.abis import EVENTS_BY_TOPIC
from src.fund_indexer.models import FundEvent, normalize_address
from src.fund_indexer.projector import project_events
from src.fund_indexer.reconciliation import reconcile
from src.fund_indexer.snapshot import SnapshotContracts, read_onchain_snapshot


logger = logging.getLogger(__name__)
INDEXER_NAME = "tokenized_csp_fund"
DEFAULT_WINDOW = 2_000
MIN_WINDOW = 10
CONFIRMATIONS = 5
SUPPORTED_INTERFACE_VERSIONS = {1}
EVENT_PAGE_SIZE = 1_000
EIP1967_IMPLEMENTATION_SLOT = int(
    "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16
)
PROXY_ROLES = {
    "fund_vault",
    "fund_share",
    "fund_accounting",
    "fund_flow_manager",
    "strategy_manager",
    "csp_adapter",
    "controller",
    "batch_settler",
}
IMMUTABLE_ROLES = {
    "claim_escrow",
    "access_manager",
    "address_book",
    "csp_valuator",
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


@dataclass(frozen=True, slots=True)
class ConfirmedHead:
    chain_id: int
    block_number: int
    block_hash: str


def _load_registries() -> list[FundRegistry]:
    client = get_client()
    rows = client.table("v2_fund_registry").select("*").eq("enabled", True).execute()
    registries = []
    for row in rows.data or []:
        if int(row["start_block"]) <= 0:
            raise ValueError(
                f"Fund {row['fund_address']} requires a positive start_block"
            )
        contracts = (
            client.table("v2_fund_contracts")
            .select(
                "contract_address,contract_role,interface_version,"
                "valid_from_block,valid_to_block,implementation_address"
            )
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
        registries.append(
            FundRegistry(
                chain_id=int(row["chain_id"]),
                fund_address=normalize_address(row["fund_address"]),
                start_block=int(row["start_block"]),
                accounting_asset=normalize_address(row["accounting_asset"]),
                weth=normalize_address(row["weth"]),
                contracts=bindings,
            )
        )
    return registries


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


def _checkpoint(registry: FundRegistry) -> dict[str, Any]:
    result = (
        get_client()
        .table("v2_indexer_checkpoints")
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


def _load_events(registry: FundRegistry) -> list[FundEvent]:
    rows = []
    offset = 0
    while True:
        result = (
            get_client()
            .table("v2_chain_events")
            .select("*")
            .eq("chain_id", registry.chain_id)
            .eq("fund_address", registry.fund_address)
            .order("block_number")
            .order("transaction_index")
            .order("log_index")
            .range(offset, offset + EVENT_PAGE_SIZE - 1)
            .execute()
        )
        page = result.data or []
        if not page:
            break
        rows.extend(page)
        offset += len(page)
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


def _fetch_window(
    w3: Web3,
    registry: FundRegistry,
    from_block: int,
    to_block: int,
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
) -> None:
    known = {event.identity: event for event in _load_events(registry)}
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
            to_block=to_block,
        )
        if canonical
        else None
    )
    projection = (
        projected.export()
        if projected is not None
        else _empty_projection(registry, to_block)
    )
    if projected is not None:
        snapshot = read_onchain_snapshot(
            w3,
            projected,
            _snapshot_contracts(registry, to_block),
            to_block,
            block_hash,
        )
        reconciliation = reconcile(projected, snapshot)
        indexed_at = _block_timestamp(w3, to_block)
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
            as_of_block=snapshot.block_number,
            as_of_block_hash=snapshot.block_hash,
            reconciled=reconciliation["passed"],
            indexed_at=indexed_at,
        )
        projected.fund.update(snapshot_state)
        projection["fund_state"][0].update(snapshot_state)
        projection["reconciliations"] = [reconciliation]
    _verify_terminal_hash(w3, to_block, block_hash)
    get_client().rpc(
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
    required = {
        "fund_vault",
        "fund_flow_manager",
        "claim_escrow",
        "csp_adapter",
        "controller",
        "batch_settler",
        "strategy_manager",
        "fund_accounting",
    }
    missing = sorted(required - by_role.keys())
    if missing:
        raise ValueError(f"Fund registry is missing reconciliation roles: {missing}")
    return SnapshotContracts(**{role: by_role[role] for role in required})


def _rewind(registry: FundRegistry, block_number: int) -> None:
    get_client().rpc(
        "v2_rewind_fund_indexer",
        {
            "p_chain_id": registry.chain_id,
            "p_fund_address": registry.fund_address,
            "p_indexer_name": INDEXER_NAME,
            "p_rewind_block": block_number,
        },
    ).execute()


def _empty_projection(registry: FundRegistry, block_number: int) -> dict[str, Any]:
    return {
        "fund_state": [
            {
                "chain_id": registry.chain_id,
                "fund_address": registry.fund_address,
                "accounting_asset": registry.accounting_asset,
                "weth": registry.weth,
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
                "as_of_block_hash": None,
                "reconciled": False,
                "indexed_at": datetime.fromtimestamp(
                    block_number, timezone.utc
                ).isoformat(),
                "nav_valid_after_block": None,
                "nav_valid_until_block": None,
                "last_event_block": block_number,
            }
        ],
        "share_balances": [],
        "redemptions": [],
        "positions": [],
        "inventory": [],
        "components": [],
        "nav_reports": [],
        "activities": [],
    }


def index_registry_once(
    w3: Web3, registry: FundRegistry, confirmed_head: ConfirmedHead
) -> int:
    if not registry.contracts:
        raise ValueError(f"Fund {registry.fund_address} has no indexed contracts")
    _validate_chain(w3, registry)
    if confirmed_head.chain_id != registry.chain_id:
        raise ValueError(
            "Confirmed head chain mismatch: "
            f"head={confirmed_head.chain_id}, registry={registry.chain_id}"
        )
    checkpoint = _checkpoint(registry)
    next_block = int(checkpoint["next_block"])
    if checkpoint.get("last_block_hash") and next_block > registry.start_block:
        parent = w3.eth.get_block(next_block - 1)
        if Web3.to_hex(parent["hash"]) != checkpoint["last_block_hash"]:
            rewind = registry.start_block
            _rewind(registry, rewind)
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
            events = _fetch_window(w3, registry, next_block, to_block)
            break
        except Exception as error:
            if window <= MIN_WINDOW:
                raise
            window = max(MIN_WINDOW, window // 2)
            logger.warning("Reducing fund log window after RPC/decode error: %s", error)
    _validate_proxy_implementations(w3, registry, to_block)
    _persist_window(w3, registry, next_block, to_block, block_hash, events)
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


def _store_confirmed_head(confirmed_head: ConfirmedHead) -> None:
    get_client().table("v2_confirmed_chain_heads").upsert(
        {
            "chain_id": confirmed_head.chain_id,
            "block_number": confirmed_head.block_number,
            "block_hash": confirmed_head.block_hash,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="chain_id",
    ).execute()


def _index_cycle(w3: Web3, registries: list[FundRegistry]) -> None:
    confirmed_heads: dict[int, ConfirmedHead] = {}
    for registry in registries:
        confirmed_head = confirmed_heads.get(registry.chain_id)
        if confirmed_head is None:
            confirmed_head = _capture_confirmed_head(w3, registry.chain_id)
            _store_confirmed_head(confirmed_head)
            confirmed_heads[registry.chain_id] = confirmed_head
        index_registry_once(w3, registry, confirmed_head)


async def run() -> None:
    if not settings.rpc_url:
        raise RuntimeError("RPC_URL is required for the tokenized fund indexer")
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    while True:
        try:
            _index_cycle(w3, _load_registries())
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Tokenized fund indexing failed")
        await asyncio.sleep(settings.event_poll_interval_seconds)
