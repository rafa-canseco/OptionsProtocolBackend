"""Durable, bounded Base snapshot collector.

The coordinator owns deduplication and publication. One process-lifetime
eth_chainId startup check runs before claims; recurrent RPC waits for a claim.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Protocol

import httpx
from hexbytes import HexBytes
from web3 import Web3

from src.config import get_tokenized_fund_rpc_url, settings
from src.db.database import get_client
from src.fund_indexer.abis import EVENTS_BY_TOPIC
from src.fund_indexer.indexer import (
    INDEXER_NAME,
    ContractBinding,
    FundRegistry,
    _decode_log,
    _empty_projection,
    _load_events,
)
from src.fund_indexer.projector import FundProjection, project_events
from src.pricing.assets import Asset
from src.pricing.deribit import get_iv_with_client
from src.fund_indexer.meta_snapshot import build_meta_plan, decode_meta
from src.fund_indexer.mm_snapshot import (
    build_covered_call_plan,
    build_csp_plan,
    decode_covered_call,
    decode_csp,
    execute_plan,
)

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30
MAX_ACTIVE_FUNDS = 3
MAX_RETRIES_PER_RPC = 1
MAX_RETRY_CALLS_PER_WINDOW = 4
MAX_SNAPSHOT_AGE_FOR_MM_SECONDS = 45
MIN_WINDOW_REMAINING_TO_CLAIM_SECONDS = 10
MAX_EVENT_BLOCK_RANGE = 2_000

_STARTUP_HEALTH: dict[tuple[str, int], bool] = {}
_STARTUP_HEALTH_LOCK = Lock()


def _set_startup_health(environment: str, chain_id: int, healthy: bool) -> None:
    with _STARTUP_HEALTH_LOCK:
        _STARTUP_HEALTH[(environment, chain_id)] = healthy


def snapshot_collector_startup_healthy(environment: str, chain_id: int) -> bool:
    with _STARTUP_HEALTH_LOCK:
        return _STARTUP_HEALTH.get((environment, chain_id), False)


@dataclass(frozen=True, slots=True)
class Claim:
    token: str
    window_id: int
    deadline: datetime


@dataclass(frozen=True, slots=True)
class BlockHeader:
    number: int
    hash: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class FundRead:
    state: dict[str, Any]
    reconciled: bool
    common: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActiveFund:
    fund_key: str
    fund_type: str
    fund_address: str
    registry: FundRegistry
    projection: FundProjection
    inputs: dict[str, Any] = field(default_factory=dict)


class Coordinator(Protocol):
    def claim(self, environment: str, chain_id: int) -> Claim | None: ...
    def funds(self, chain_id: int) -> list[ActiveFund]: ...
    def event_start(self, funds: list[ActiveFund]) -> int: ...
    def build_ingestions(
        self, funds: list[ActiveFund], logs: list[dict[str, Any]], header: BlockHeader
    ) -> list[dict[str, Any]]: ...
    def schedule_backfill(
        self,
        environment: str,
        chain_id: int,
        start: int,
        end: int,
        reason: str = "event_gap_exceeds_2000",
    ) -> None: ...
    def reserve_retry(self, claim: Claim, environment: str, chain_id: int) -> bool: ...
    def publish(
        self,
        claim: Claim,
        environment: str,
        chain_id: int,
        header: BlockHeader,
        common: dict[str, Any],
        funds: list[dict[str, Any]],
        ingestions: list[dict[str, Any]],
    ) -> int: ...
    def fail(
        self, claim: Claim, environment: str, chain_id: int, code: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MarketObservation:
    iv: float
    iv_source: str
    observed_at: int


class MarketDataReader(Protocol):
    def read(self, deadline: datetime) -> MarketObservation: ...


class SnapshotRPC(Protocol):
    def validate_chain(self, chain_id: int) -> None: ...
    def safe_header(self) -> BlockHeader: ...
    def event_logs(
        self, funds: list[ActiveFund], from_block: int, to_block: int
    ) -> list[dict[str, Any]]: ...
    def fund_state(self, fund: ActiveFund, header: BlockHeader) -> FundRead: ...


class DeribitMarketDataReader:
    @staticmethod
    async def _read_iv(timeout: float):
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await get_iv_with_client(Asset.ETH, client)

    def read(self, deadline: datetime) -> MarketObservation:
        if datetime.now(timezone.utc) >= deadline:
            raise RuntimeError("Offchain market read deadline expired")
        started = time.monotonic()
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        timeout = max(0.001, remaining)
        result = asyncio.run(asyncio.wait_for(self._read_iv(timeout), timeout=timeout))
        if datetime.now(timezone.utc) >= deadline:
            raise RuntimeError("Offchain market read exceeded snapshot deadline")
        logger.info(
            "snapshot traffic_class=offchain_market provider=%s latency_ms=%d",
            result.source,
            round((time.monotonic() - started) * 1000),
        )
        return MarketObservation(
            iv=float(result.value),
            iv_source=str(result.source),
            observed_at=int(time.time()),
        )


class SupabaseCoordinator:
    def __init__(self, client=None):
        self.client = client or get_client()
        self.codec_w3 = Web3()

    def claim(self, environment: str, chain_id: int) -> Claim | None:
        result = self.client.rpc(
            "v2_claim_snapshot_window",
            {"p_environment": environment, "p_chain_id": chain_id},
        ).execute()
        row = (result.data or [None])[0]
        if not row:
            return None
        return Claim(
            token=str(row["claim_token"]),
            window_id=int(row["window_id"]),
            deadline=_timestamp(row["publish_deadline"]),
        )

    def funds(self, chain_id: int) -> list[ActiveFund]:
        result = self.client.rpc(
            "v2_get_snapshot_inputs", {"p_chain_id": chain_id}
        ).execute()
        bundle = result.data
        if isinstance(bundle, list):
            bundle = bundle[0] if bundle else None
        if not isinstance(bundle, dict) or int(bundle.get("maker_count", 0)) != 1:
            raise RuntimeError("Exactly one active Base MM principal is required")
        mm_address = str(bundle.get("mm_address", "")).lower()
        usdc = settings.usdc_address.lower()
        spender = settings.margin_pool_address.lower()
        if not all(Web3.is_address(value) for value in (mm_address, usdc, spender)):
            raise RuntimeError("Snapshot common address configuration is invalid")
        quotes = bundle.get("quotes") or []
        if any(
            row.get("deployment_status")
            not in {"virtual", "creating", "ready", "failed"}
            for row in quotes
        ):
            raise RuntimeError("Active quote lifecycle binding is missing")
        shared = {
            "common": {
                "mm_address": mm_address,
                "usdc_address": usdc,
                "allowance_spender": spender,
            },
            "quotes": quotes,
            "available_otokens": bundle.get("available_otokens") or [],
            "chain_id": chain_id,
        }
        rows = bundle.get("funds") or []
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ACTIVE_FUNDS:
            raise RuntimeError("Invalid active fund count")
        funds = [self._fund_from_bundle(item, shared) for item in rows]
        if len({fund.fund_type for fund in funds}) != len(funds):
            raise RuntimeError("Active fund_type values must be unique")
        return funds

    def _fund_from_bundle(
        self, bundle: dict[str, Any], shared: dict[str, Any]
    ) -> ActiveFund:
        row = bundle["registry"]
        bindings = tuple(
            ContractBinding(
                address=str(item["contract_address"]).lower(),
                role=str(item["contract_role"]),
                interface_version=int(item["interface_version"]),
                valid_from_block=int(item["valid_from_block"]),
                valid_to_block=(
                    int(item["valid_to_block"])
                    if item.get("valid_to_block") is not None
                    else None
                ),
                implementation_address=(
                    str(item["implementation_address"]).lower()
                    if item.get("implementation_address")
                    else None
                ),
            )
            for item in bundle.get("contracts") or []
        )
        registry = FundRegistry(
            chain_id=int(row["chain_id"]),
            fund_address=str(row["fund_address"]).lower(),
            start_block=int(row["start_block"]),
            accounting_asset=str(row["accounting_asset"]).lower(),
            weth=str(row["weth"]).lower(),
            contracts=bindings,
            strategy_kind=str(row.get("strategy_kind", "csp")),
            quote_asset=(
                str(row["quote_asset"]).lower() if row.get("quote_asset") else None
            ),
        )
        if not bundle.get("state"):
            raise RuntimeError(f"Fund {row['fund_key']} has no durable state")
        state = dict(bundle["state"])
        positions = bundle.get("positions") or []
        inventory = bundle.get("inventory") or []
        state.update(
            chain_id=registry.chain_id,
            fund_address=registry.fund_address,
            accounting_asset=registry.accounting_asset,
            weth=registry.weth,
            strategy_kind=registry.strategy_kind,
            quote_asset=registry.quote_asset,
        )
        projection = FundProjection(
            fund=state,
            positions={
                (str(item["adapter_address"]).lower(), int(item["position_id"])): item
                for item in positions
            },
            inventory={
                (str(item["asset_address"]).lower(), str(item["bucket"])): int(
                    item["amount"]
                )
                for item in inventory
            },
        )
        inputs = dict(shared)
        inputs.update(
            checkpoint=bundle.get("checkpoint"),
            redemption_batches=bundle.get("redemption_batches") or [],
            meta_state=bundle.get("meta_state"),
            meta_lanes=bundle.get("meta_lanes") or [],
            meta_tranches=bundle.get("meta_tranches") or [],
            meta_lots=bundle.get("meta_lots") or [],
            meta_nav=bundle.get("meta_nav"),
            meta_lane_valuations=bundle.get("meta_lane_valuations") or [],
        )
        if registry.strategy_kind == "csp":
            inputs.update(
                valuator_names=(
                    "interface_version",
                    "policy_version",
                    "model_version",
                    "liability_buffer_bps",
                    "max_divergence_bps",
                    "observation_quorum",
                ),
                valuator_calls=(
                    ("interface_version", "interfaceVersion()", "uint64", ()),
                    ("policy_version", "valuationPolicyVersion()", "uint64", ()),
                    ("model_version", "requiredModelVersion()", "uint64", ()),
                    ("liability_buffer_bps", "liabilityBufferBps()", "uint16", ()),
                    (
                        "max_divergence_bps",
                        "maxObservationDivergenceBps()",
                        "uint16",
                        (),
                    ),
                    ("observation_quorum", "observationQuorum()", "uint16", ()),
                ),
            )
        elif registry.strategy_kind == "covered_call":
            inputs.update(
                observers=(
                    "0x3b7f3e42eaCB2E0361aE41e426ea65C6f7896D1e",
                    "0x62A7e8c11E4eFc8ed696b2A08D9ccfC339424754",
                ),
                valuator_names=(
                    "interface_version",
                    "policy_version",
                    "model_version",
                    "liability_buffer_bps",
                    "max_divergence_bps",
                    "observation_quorum",
                    "max_observation_window",
                    "spot_feed",
                    "spot_feed_decimals",
                    "max_spot_staleness",
                ),
                valuator_calls=(
                    ("interface_version", "interfaceVersion()", "uint64", ()),
                    ("policy_version", "valuationPolicyVersion()", "uint64", ()),
                    ("model_version", "requiredModelVersion()", "uint64", ()),
                    ("liability_buffer_bps", "liabilityBufferBps()", "uint16", ()),
                    (
                        "max_divergence_bps",
                        "maxObservationDivergenceBps()",
                        "uint16",
                        (),
                    ),
                    ("observation_quorum", "observationQuorum()", "uint16", ()),
                    ("max_observation_window", "maxObservationWindow()", "uint32", ()),
                    ("spot_feed", "spotFeed()", "address", ()),
                    ("spot_feed_decimals", "spotFeedDecimals()", "uint8", ()),
                    ("max_spot_staleness", "maxSpotStaleness()", "uint32", ()),
                ),
            )
        return ActiveFund(
            fund_key=str(row["fund_key"]),
            fund_type=registry.strategy_kind,
            fund_address=registry.fund_address,
            registry=registry,
            projection=projection,
            inputs=inputs,
        )

    def event_start(self, funds: list[ActiveFund]) -> int:
        return min(
            int(
                (fund.inputs.get("checkpoint") or {}).get(
                    "next_block", fund.registry.start_block
                )
            )
            for fund in funds
        )

    def build_ingestions(
        self,
        funds: list[ActiveFund],
        logs: list[dict[str, Any]],
        header: BlockHeader,
    ) -> list[dict[str, Any]]:
        ingestions = []
        for fund in funds:
            from_block = int(
                (fund.inputs.get("checkpoint") or {}).get(
                    "next_block", fund.registry.start_block
                )
            )
            if from_block > header.number + 1:
                raise RuntimeError("Fund event checkpoint is ahead of safe block")
            if from_block == header.number + 1:
                continue
            bindings: dict[str, list[ContractBinding]] = {}
            for binding in fund.registry.contracts:
                if binding.valid_from_block <= header.number and (
                    binding.valid_to_block is None
                    or binding.valid_to_block >= from_block
                ):
                    bindings.setdefault(binding.address, []).append(binding)
            if fund.fund_type == "meta_wheel":
                for lane in fund.inputs.get("meta_lanes") or []:
                    address = str(lane["child_vault"]).lower()
                    bindings.setdefault(address, []).append(
                        ContractBinding(
                            address=address,
                            role="wheel_child_lane",
                            interface_version=1,
                            valid_from_block=fund.registry.start_block,
                            valid_to_block=None,
                        )
                    )
            decoded = []
            for raw in logs:
                address = str(raw["address"]).lower()
                block_number = int(raw["blockNumber"], 16)
                if address not in bindings or block_number < from_block:
                    continue
                normalized = {
                    **raw,
                    "blockNumber": block_number,
                    "transactionIndex": int(raw["transactionIndex"], 16),
                    "logIndex": int(raw["logIndex"], 16),
                    "blockHash": HexBytes(raw["blockHash"]),
                    "transactionHash": HexBytes(raw["transactionHash"]),
                    "topics": [HexBytes(topic) for topic in raw["topics"]],
                    "data": HexBytes(raw["data"]),
                }
                event = _decode_log(self.codec_w3, fund.registry, bindings, normalized)
                if event is not None:
                    decoded.append(event)
            known = {
                event.identity: event
                for event in _load_events(fund.registry, self.client)
            }
            for event in decoded:
                previous = known.get(event.identity)
                if previous is not None and previous.block_hash != event.block_hash:
                    raise RuntimeError("Indexed event identity changed block hash")
                known[event.identity] = event
            canonical = sorted(
                known.values(),
                key=lambda event: (
                    event.block_number,
                    event.transaction_index,
                    event.log_index,
                ),
            )
            if canonical:
                projected = project_events(
                    canonical,
                    fund.registry.accounting_asset,
                    fund.registry.weth,
                    strategy_kind=fund.registry.strategy_kind,
                    quote_asset=fund.registry.quote_asset,
                    to_block=header.number,
                )
                projection = projected.export()
                fund.projection.fund = projected.fund
                fund.projection.positions = projected.positions
                fund.projection.inventory = projected.inventory
                fund.projection.adapters = projected.adapters
                fund.projection.adapter_nonces = projected.adapter_nonces
                if fund.fund_type == "meta_wheel":
                    fund.inputs["meta_state"] = (
                        projection.get("wheel_state") or [None]
                    )[0]
                    fund.inputs["meta_lanes"] = projection.get("wheel_lanes") or []
                    fund.inputs["meta_tranches"] = (
                        projection.get("wheel_tranches") or []
                    )
                    fund.inputs["meta_lots"] = (
                        projection.get("wheel_assignment_lots") or []
                    )
            else:
                projection = _empty_projection(
                    fund.registry,
                    header.number,
                    block_hash=header.hash,
                    indexed_at=header.timestamp.isoformat(),
                )
            ingestions.append(
                {
                    "chain_id": fund.registry.chain_id,
                    "fund_address": fund.fund_address,
                    "indexer_name": INDEXER_NAME,
                    "from_block": from_block,
                    "to_block": header.number,
                    "last_block_hash": header.hash,
                    "events": [event.as_row() for event in decoded],
                    "projection": projection,
                }
            )
        return ingestions

    def schedule_backfill(
        self,
        environment: str,
        chain_id: int,
        start: int,
        end: int,
        reason: str = "event_gap_exceeds_2000",
    ) -> None:
        self.client.table("v2_snapshot_event_backfills").upsert(
            {
                "environment": environment,
                "chain_id": chain_id,
                "from_block": start,
                "to_block": end,
                "reason": reason,
                "status": "pending",
            },
            on_conflict="environment,chain_id,from_block,to_block",
        ).execute()

    def reserve_retry(self, claim: Claim, environment: str, chain_id: int) -> bool:
        result = self.client.rpc(
            "v2_reserve_snapshot_retry",
            {
                "p_environment": environment,
                "p_chain_id": chain_id,
                "p_window_id": claim.window_id,
                "p_claim_token": claim.token,
            },
        ).execute()
        return bool(result.data)

    def publish(
        self,
        claim: Claim,
        environment: str,
        chain_id: int,
        header: BlockHeader,
        common: dict[str, Any],
        funds: list[dict[str, Any]],
        ingestions: list[dict[str, Any]],
    ) -> int:
        result = self.client.rpc(
            "v2_publish_snapshot",
            {
                "p_environment": environment,
                "p_chain_id": chain_id,
                "p_window_id": claim.window_id,
                "p_claim_token": claim.token,
                "p_snapshot_block": header.number,
                "p_snapshot_block_hash": header.hash,
                "p_snapshot_block_timestamp": header.timestamp.isoformat(),
                "p_common": common,
                "p_funds": funds,
                "p_ingestions": ingestions,
            },
        ).execute()
        return int(result.data)

    def fail(self, claim: Claim, environment: str, chain_id: int, code: str) -> None:
        self.client.rpc(
            "v2_fail_snapshot_window",
            {
                "p_environment": environment,
                "p_chain_id": chain_id,
                "p_window_id": claim.window_id,
                "p_claim_token": claim.token,
                "p_failure_code": code,
            },
        ).execute()


class Web3SnapshotRPC:
    def __init__(self, rpc_url: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.deadline: datetime | None = None
        self.validated_chain_id: int | None = None
        self.request_counts = {"startup_rpc": 0, "recurrent_rpc": 0, "backfill_rpc": 0}

    def validate_chain(self, chain_id: int) -> None:
        if self.validated_chain_id is not None:
            if self.validated_chain_id != chain_id:
                raise RuntimeError("Snapshot RPC startup validation failed")
            return
        self.request_counts["startup_rpc"] += 1
        try:
            self.w3.provider._request_kwargs["timeout"] = 5.0
            response = self.w3.provider.make_request("eth_chainId", [])
            result = response.get("result") if isinstance(response, dict) else None
            if not isinstance(result, str) or int(result, 16) != chain_id:
                raise ValueError
        except Exception:
            raise RuntimeError("Snapshot RPC startup validation failed") from None
        self.validated_chain_id = chain_id
        logger.info("snapshot traffic_class=startup_rpc validation=ok")

    def set_deadline(self, deadline: datetime) -> None:
        self.deadline = deadline

    def _bound_http_timeout(self) -> None:
        if self.deadline is None:
            raise RuntimeError("Snapshot RPC deadline is not set")
        remaining = (self.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise RuntimeError("Snapshot RPC deadline expired")
        self.w3.provider._request_kwargs["timeout"] = min(5.0, remaining)

    def safe_header(self) -> BlockHeader:
        self._bound_http_timeout()
        self.request_counts["recurrent_rpc"] += 1
        response = self.w3.provider.make_request(
            "eth_getBlockByNumber", ["safe", False]
        )
        if "error" in response or not response.get("result"):
            raise RuntimeError("Safe block RPC failed")
        block = response["result"]
        return BlockHeader(
            number=int(block["number"], 16),
            hash=str(block["hash"]).lower(),
            timestamp=datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc),
        )

    def event_logs(
        self,
        funds: list[ActiveFund],
        from_block: int,
        to_block: int,
        *,
        traffic_class: str = "recurrent_rpc",
    ) -> list[dict[str, Any]]:
        self._bound_http_timeout()
        self.request_counts[traffic_class] += 1
        addresses = {
            binding.address
            for fund in funds
            for binding in fund.registry.contracts
            if binding.valid_from_block <= to_block
            and (binding.valid_to_block is None or binding.valid_to_block >= from_block)
        }
        addresses.update(
            str(lane["child_vault"]).lower()
            for fund in funds
            for lane in (fund.inputs.get("meta_lanes") or [])
        )
        response = self.w3.provider.make_request(
            "eth_getLogs",
            [
                {
                    "address": [
                        Web3.to_checksum_address(address)
                        for address in sorted(addresses)
                    ],
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "topics": [list(EVENTS_BY_TOPIC)],
                }
            ],
        )
        if "error" in response or response.get("result") is None:
            raise RuntimeError("Bounded event RPC failed")
        return list(response["result"])

    def fund_state(self, fund: ActiveFund, header: BlockHeader) -> FundRead:
        self._bound_http_timeout()
        self.request_counts["recurrent_rpc"] += 1
        include_common = bool(fund.inputs.get("include_common"))
        if fund.fund_type == "csp":
            calls, metadata = build_csp_plan(fund, include_common=include_common)
            result = execute_plan(self.w3, calls, header.hash)
            state, common = decode_csp(
                fund, result, metadata, include_common=include_common
            )
        elif fund.fund_type == "covered_call":
            calls, metadata = build_covered_call_plan(
                fund, include_common=include_common
            )
            result = execute_plan(self.w3, calls, header.hash)
            state, common = decode_covered_call(
                fund, result, metadata, include_common=include_common
            )
        else:
            calls, metadata = build_meta_plan(fund, include_common=include_common)
            result = execute_plan(self.w3, calls, header.hash)
            state, common = decode_meta(
                fund, result, metadata, header, include_common=include_common
            )
        state.get("operations", {})["latest_block"] = header.number
        return FundRead(state=state, reconciled=True, common=common)


MARKET_MAKER_KEYS = {"mm_address", "usdc_balance", "usdc_allowance", "maker_nonce"}
OPERATIONS_KEYS = {
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
CSP_ALLOCATOR_KEYS = {
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
COVERED_CALL_ALLOCATOR_KEYS = {
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
META_WHEEL_ALLOCATOR_KEYS = {"wheel_snapshot", "wheel_quotes"}
COMMON_MARKET_MAKER_KEYS = {
    "mm_address",
    "usdc_address",
    "allowance_spender",
    "usdc_balance_raw",
    "usdc_allowance_raw",
    "maker_nonce",
}
COMMON_MARKET_KEYS = {
    "asset",
    "spot",
    "iv",
    "iv_source",
    "observed_at",
    "protocol_fee_bps",
    "available_otokens",
}
COMMON_QUOTE_KEYS = {
    "asset",
    "chain",
    "is_put",
    "created_at",
    "deadline",
    "expiry",
    "strike_price",
    "deployment_status",
    "otoken_address",
    "bid_price",
    "quote_id",
    "max_amount",
    "maker_nonce",
    "signature",
}
COMMON_OTOKEN_KEYS = {"address", "strike_price", "expiry", "is_put"}
SERIES_KEYS = {
    "is_put",
    "underlying",
    "strike_asset",
    "collateral_asset",
    "expiry",
    "strike_price",
}
QUOTE_STATE_KEYS = {"filled_amount", "cancelled"}
WHEEL_LANE_KEYS = {
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
WHEEL_QUOTE_KEYS = {
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
WHEEL_PENDING_KEYS = {"tranche_id", "state_nonce", "pending_usdc", "principal_usdc"}
WHEEL_LOT_KEYS = {
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
WHEEL_SNAPSHOT_KEYS = {
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


def required_common_complete(common: dict[str, Any]) -> bool:
    market_maker = common.get("market_maker")
    market = common.get("market")
    quotes = common.get("quotes")
    return (
        set(common) == {"market", "quotes", "market_maker"}
        and isinstance(market, dict)
        and set(market) == COMMON_MARKET_KEYS
        and isinstance(market.get("available_otokens"), list)
        and all(
            isinstance(item, dict) and set(item) == COMMON_OTOKEN_KEYS
            for item in market["available_otokens"]
        )
        and isinstance(quotes, list)
        and all(
            isinstance(item, dict) and set(item) == COMMON_QUOTE_KEYS for item in quotes
        )
        and isinstance(market_maker, dict)
        and set(market_maker) == COMMON_MARKET_MAKER_KEYS
        and all(
            Web3.is_address(str(market_maker[key]))
            for key in ("mm_address", "usdc_address", "allowance_spender")
        )
        and all(
            isinstance(market_maker[key], int) and market_maker[key] >= 0
            for key in ("usdc_balance_raw", "usdc_allowance_raw", "maker_nonce")
        )
    )


def required_state_complete(fund_type: str, state: dict[str, Any]) -> bool:
    """Validate the exact canonical MM recurrent state shape."""
    expected_sections = (
        {"allocator"} if fund_type == "meta_wheel" else {"allocator", "operations"}
    )
    if set(state) != expected_sections or not isinstance(state.get("allocator"), dict):
        return False
    allocator = state["allocator"]
    required_allocator = {
        "csp": CSP_ALLOCATOR_KEYS,
        "covered_call": COVERED_CALL_ALLOCATOR_KEYS,
        "meta_wheel": META_WHEEL_ALLOCATOR_KEYS,
    }.get(fund_type)
    if required_allocator is None or set(allocator) != required_allocator:
        return False
    if fund_type == "meta_wheel":
        wheel = allocator["wheel_snapshot"]
        quotes = allocator["wheel_quotes"]
        return (
            isinstance(wheel, dict)
            and set(wheel) == WHEEL_SNAPSHOT_KEYS
            and all(
                isinstance(item, dict) and set(item) == WHEEL_LANE_KEYS
                for item in (*wheel["csp_lanes"], *wheel["call_lanes"])
            )
            and all(
                isinstance(item, dict) and set(item) == WHEEL_PENDING_KEYS
                for item in wheel["pending_csp_tranches"]
            )
            and all(
                isinstance(item, dict) and set(item) == WHEEL_LOT_KEYS
                for item in wheel["assignment_lots"]
            )
            and isinstance(quotes, list)
            and all(
                isinstance(item, dict) and set(item) == WHEEL_QUOTE_KEYS
                for item in quotes
            )
        )
    operations = state.get("operations")
    series = allocator["series"]
    quote_states = allocator.get("quote_states", {})
    return (
        isinstance(operations, dict)
        and set(operations) == OPERATIONS_KEYS
        and isinstance(series, dict)
        and all(
            isinstance(item, dict) and set(item) == SERIES_KEYS
            for item in series.values()
        )
        and (
            fund_type != "csp"
            or (
                isinstance(quote_states, dict)
                and all(
                    isinstance(item, dict) and set(item) == QUOTE_STATE_KEYS
                    for item in quote_states.values()
                )
            )
        )
    )


def checkpoint_hash_mode(checkpoint_block: int, snapshot_block: int) -> str:
    distance = snapshot_block - checkpoint_block
    if distance == 0:
        return "header"
    if 1 <= distance <= 256:
        return "blockhash"
    if distance > 256:
        return "backfill"
    return "future"


def next_window_delay(epoch_seconds: float) -> float:
    """Align retries to the next DB-sized bucket instead of phase-locking."""
    return max(
        0.05, REFRESH_INTERVAL_SECONDS - epoch_seconds % REFRESH_INTERVAL_SECONDS + 0.05
    )


def event_range(last_indexed_block: int, snapshot_block: int) -> tuple[str, int, int]:
    """Return a bounded foreground range or explicit backfill work."""
    start = last_indexed_block + 1
    if snapshot_block < start:
        return "none", start, snapshot_block
    kind = (
        "foreground"
        if snapshot_block - start + 1 <= MAX_EVENT_BLOCK_RANGE
        else "backfill"
    )
    return kind, start, snapshot_block


class SnapshotCollector:
    def __init__(
        self,
        coordinator: Coordinator,
        rpc: SnapshotRPC,
        environment: str,
        chain_id: int,
        *,
        market_reader: MarketDataReader | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self.coordinator = coordinator
        self.rpc = rpc
        self.environment = environment
        self.chain_id = chain_id
        self.market_reader = market_reader
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.jitter = jitter
        self._startup_validated = False

    def validate_startup(self) -> None:
        if self._startup_validated:
            return
        try:
            self.rpc.validate_chain(self.chain_id)
        except Exception:
            _set_startup_health(self.environment, self.chain_id, False)
            raise RuntimeError(
                "Snapshot collector startup RPC validation failed"
            ) from None
        self._startup_validated = True
        _set_startup_health(self.environment, self.chain_id, True)

    @staticmethod
    def _transient(error: Exception) -> bool:
        return isinstance(error, (TimeoutError, httpx.TransportError, OSError)) or (
            "RPC failed" in str(error)
        )

    def _call(self, claim: Claim, operation: Callable[[], Any]) -> Any:
        if self.now() >= claim.deadline:
            raise RuntimeError("Snapshot RPC deadline expired")
        try:
            return operation()
        except Exception as error:
            if not self._transient(error) or self.now() >= claim.deadline:
                raise
            if not self.coordinator.reserve_retry(
                claim, self.environment, self.chain_id
            ):
                raise
            remaining = (claim.deadline - self.now()).total_seconds()
            delay = min(1.0 + self.jitter(), max(0.0, remaining))
            if delay:
                self.sleep(delay)
            if self.now() >= claim.deadline:
                raise RuntimeError("Snapshot retry deadline expired")
            return operation()

    def collect_once(self) -> int | None:
        try:
            self.validate_startup()
        except Exception:
            logger.warning("Snapshot collector startup_rpc validation failed")
            return None
        try:
            claim = self.coordinator.claim(self.environment, self.chain_id)
        except Exception:
            logger.warning("Snapshot claim unavailable; recurrent RPC skipped")
            return None
        if claim is None:
            return None
        set_deadline = getattr(self.rpc, "set_deadline", None)
        if set_deadline is not None:
            set_deadline(claim.deadline)
        try:
            funds = self.coordinator.funds(self.chain_id)
            if not funds or len(funds) > MAX_ACTIVE_FUNDS:
                raise RuntimeError("Invalid active fund count")
            header = self._call(claim, self.rpc.safe_header)
            event_start_block = self.coordinator.event_start(funds)
            event_kind, _, _ = event_range(event_start_block - 1, header.number)
            if event_kind == "backfill":
                self.coordinator.schedule_backfill(
                    self.environment,
                    self.chain_id,
                    event_start_block,
                    header.number,
                )
                raise RuntimeError("Explicit event backfill is required")
            event_logs = (
                self._call(
                    claim,
                    lambda: self.rpc.event_logs(
                        funds, event_start_block, header.number
                    ),
                )
                if event_kind == "foreground"
                else []
            )
            ingestions = self.coordinator.build_ingestions(funds, event_logs, header)
            if self.market_reader is None:
                raise RuntimeError("Required offchain market reader is unavailable")
            market = self.market_reader.read(claim.deadline)
            for fund in funds:
                fund.inputs["market"] = market
                fund.inputs["header_timestamp"] = int(header.timestamp.timestamp())
            checkpoint_hashes = {}
            for selected in funds:
                checkpoint = selected.inputs.get("checkpoint") or {}
                if (
                    checkpoint.get("last_block_hash")
                    and int(checkpoint.get("next_block", 0))
                    > selected.registry.start_block
                ):
                    checkpoint_block = int(checkpoint["next_block"]) - 1
                    expected_hash = str(checkpoint["last_block_hash"]).lower()
                    mode = checkpoint_hash_mode(checkpoint_block, header.number)
                    if mode == "header":
                        if expected_hash != header.hash.lower():
                            raise RuntimeError("Fund event checkpoint is not canonical")
                    elif mode == "blockhash":
                        checkpoint_hashes[selected.fund_address] = (
                            checkpoint_block,
                            expected_hash,
                        )
                    else:
                        self.coordinator.schedule_backfill(
                            self.environment,
                            self.chain_id,
                            min(fund.registry.start_block for fund in funds),
                            header.number,
                            "checkpoint_reorg_rebuild",
                        )
                        raise RuntimeError("Explicit event backfill is required")
            funds[0].inputs["checkpoint_hashes"] = checkpoint_hashes
            envelope_funds = []
            common = None
            for index, fund in enumerate(funds):
                fund.inputs["include_common"] = index == 0
                read = self._call(
                    claim, lambda selected=fund: self.rpc.fund_state(selected, header)
                )
                if not read.reconciled or not required_state_complete(
                    fund.fund_type, read.state
                ):
                    raise RuntimeError("Required recurrent snapshot fields are missing")
                if index == 0:
                    common = read.common
                elif read.common is not None:
                    raise RuntimeError(
                        "Shared snapshot state appeared outside first fund"
                    )
                envelope_funds.append(
                    {
                        "fund_key": fund.fund_key,
                        "fund_type": fund.fund_type,
                        "fund_address": fund.fund_address,
                        "state": read.state,
                    }
                )
            if common is None or not required_common_complete(common):
                raise RuntimeError("Required common snapshot fields are missing")
            common_fee = int(common["market"]["protocol_fee_bps"])
            fund_fees = {
                int(
                    item["state"]["allocator"].get(
                        "protocol_fee_bps",
                        item["state"]["allocator"]
                        .get("wheel_snapshot", {})
                        .get("protocol_premium_fee_bps", -1),
                    )
                )
                for item in envelope_funds
            }
            if fund_fees != {common_fee}:
                raise RuntimeError("Fund protocol fee snapshots are inconsistent")
            return self.coordinator.publish(
                claim,
                self.environment,
                self.chain_id,
                header,
                common,
                envelope_funds,
                ingestions,
            )
        except Exception as exc:
            try:
                self.coordinator.fail(
                    claim, self.environment, self.chain_id, type(exc).__name__
                )
            except Exception:
                logger.warning("Snapshot failure could not be persisted")
            logger.warning("Snapshot window failed: %s", type(exc).__name__)
            return None


def _timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def create_validated_rpc(
    environment: str, chain_id: int, rpc_url: str | None = None
) -> Web3SnapshotRPC:
    resolved_url = rpc_url or get_tokenized_fund_rpc_url()
    if not resolved_url:
        _set_startup_health(environment, chain_id, False)
        raise RuntimeError(
            "TOKENIZED_FUND_RPC_URL or RPC_URL is required when "
            "RPC_SNAPSHOT_COLLECTOR_ENABLED=true"
        )
    rpc = Web3SnapshotRPC(resolved_url)
    try:
        rpc.validate_chain(chain_id)
    except Exception:
        _set_startup_health(environment, chain_id, False)
        raise RuntimeError("Snapshot collector startup RPC validation failed") from None
    _set_startup_health(environment, chain_id, True)
    return rpc


async def run(rpc: Web3SnapshotRPC | None = None) -> None:
    rpc = rpc or create_validated_rpc(settings.app_env, settings.chain_id)
    try:
        rpc.validate_chain(settings.chain_id)
    except Exception:
        _set_startup_health(settings.app_env, settings.chain_id, False)
        raise RuntimeError("Snapshot collector startup RPC validation failed") from None
    _set_startup_health(settings.app_env, settings.chain_id, True)
    coordinator = SupabaseCoordinator()
    funds = await asyncio.to_thread(coordinator.funds, settings.chain_id)
    if len(funds) > MAX_ACTIVE_FUNDS:
        raise RuntimeError(f"At most {MAX_ACTIVE_FUNDS} active funds are supported")
    collector = SnapshotCollector(
        coordinator,
        rpc,
        settings.app_env,
        settings.chain_id,
        market_reader=DeribitMarketDataReader(),
    )
    while True:
        try:
            await asyncio.to_thread(collector.collect_once)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Snapshot collector supervision caught an error")
        await asyncio.sleep(next_window_delay(time.time()))
