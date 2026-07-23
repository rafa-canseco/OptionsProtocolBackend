"""DB-first product service for tokenized CSP funds."""

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from src.config import settings
from src.db.database import get_client
from src.models.csp_vault import (
    ActionAvailability,
    ActivityItem,
    ActivityResponse,
    FundActions,
    FundComposition,
    FundConfigResponse,
    FundListResponse,
    FundPositionResponse,
    FundRegistryItem,
    FundStatus,
    FundSummaryResponse,
    NavWindow,
    RedemptionView,
    TokenMetadata,
    TrustedContract,
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
REQUIRED_TRUSTED_ROLES = PROXY_ROLES | {
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


class UnknownFundError(LookupError):
    """The requested fund key is not registered."""


class FundRepository(Protocol):
    def registries(self) -> list[dict[str, Any]]: ...
    def state(self, chain_id: int, fund: str) -> dict[str, Any] | None: ...
    def inventory(self, chain_id: int, fund: str) -> list[dict[str, Any]]: ...
    def position(self, chain_id: int, fund: str, wallet: str) -> dict[str, Any]: ...
    def contracts(self, chain_id: int, fund: str) -> list[dict[str, Any]]: ...
    def confirmed_head(self, chain_id: int) -> dict[str, Any] | None: ...
    def activity(
        self, chain_id: int, fund: str, cursor: tuple[int, int] | None, limit: int
    ) -> list[dict[str, Any]]: ...


class SupabaseFundRepository:
    def registries(self) -> list[dict[str, Any]]:
        result = (
            get_client()
            .table("v2_fund_registry")
            .select("*")
            .eq("enabled", True)
            .execute()
        )
        return result.data or []

    def state(self, chain_id: int, fund: str) -> dict[str, Any] | None:
        rows = self._fund_query("v2_fund_state", chain_id, fund).limit(1).execute()
        return rows.data[0] if rows.data else None

    def inventory(self, chain_id: int, fund: str) -> list[dict[str, Any]]:
        return (
            self._fund_query("v2_fund_inventory", chain_id, fund).execute().data or []
        )

    def position(self, chain_id: int, fund: str, wallet: str) -> dict[str, Any]:
        balance = (
            self._fund_query("v2_share_balances", chain_id, fund)
            .eq("wallet_address", wallet)
            .limit(1)
            .execute()
        )
        redemption = (
            self._fund_query("v2_redemptions", chain_id, fund)
            .eq("controller_address", wallet)
            .limit(1)
            .execute()
        )
        redemption_row = dict(redemption.data[0]) if redemption.data else {}
        batch_state = (
            self._fund_query("v2_redemption_batch_states", chain_id, fund)
            .eq("controller_address", wallet)
            .limit(1)
            .execute()
        )
        if batch_state.data:
            state = batch_state.data[0]
            redemption_row.update(
                latest_batch_id=state["latest_batch_id"],
                latest_batch_processing=state["processing"],
                latest_batch_unwind_committed=state["unwind_committed"],
            )
        return {
            "shares": balance.data[0]["shares"] if balance.data else 0,
            "redemption": redemption_row,
        }

    def contracts(self, chain_id: int, fund: str) -> list[dict[str, Any]]:
        query = self._fund_query("v2_fund_contracts", chain_id, fund)
        return query.execute().data or []

    def confirmed_head(self, chain_id: int) -> dict[str, Any] | None:
        result = (
            get_client()
            .table("v2_confirmed_chain_heads")
            .select("*")
            .eq("chain_id", chain_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def activity(
        self, chain_id: int, fund: str, cursor: tuple[int, int] | None, limit: int
    ) -> list[dict[str, Any]]:
        query = self._fund_query("v2_fund_activity", chain_id, fund)
        if cursor:
            block, log = cursor
            query = query.or_(
                f"block_number.lt.{block},and(block_number.eq.{block},log_index.lt.{log})"
            )
        result = query.order("block_number", desc=True).order("log_index", desc=True)
        return result.limit(limit).execute().data or []

    @staticmethod
    def _fund_query(table: str, chain_id: int, fund: str):
        return (
            get_client()
            .table(table)
            .select("*")
            .eq("chain_id", chain_id)
            .eq("fund_address", fund)
        )


class FundService:
    def __init__(self, repository: FundRepository, now=None):
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))

    def list_funds(self) -> FundListResponse:
        return FundListResponse(
            funds=[
                self._registry(row)
                for row in self.repository.registries()
                if row.get("enabled", False)
            ]
        )

    def summary(self, fund_key: str) -> FundSummaryResponse:
        row = self._find(fund_key)
        state = self.repository.state(int(row["chain_id"]), row["fund_address"]) or {}
        inventory = self.repository.inventory(int(row["chain_id"]), row["fund_address"])
        context = self._write_context(row, state)
        stale = context["stale"]
        actions = self._actions(row, state, common=context["reason"])
        net_assets = int(state.get("net_assets", 0))
        supply = int(state.get("share_supply", 0))
        virtual = int(state.get("virtual_shares", 0))
        denominator = supply + virtual
        share_price = (
            (net_assets + 1) * 10 ** int(row["share_decimals"]) // denominator
            if denominator
            else 0
        )
        amounts = {
            (item["asset_address"], item["bucket"]): item["amount"]
            for item in inventory
        }
        return FundSummaryResponse(
            fund=self._registry(row),
            net_assets=str(net_assets),
            share_supply=str(supply),
            virtual_shares=str(virtual),
            share_price_assets=str(share_price),
            composition=FundComposition(
                idle_assets=str(state.get("accounted_idle_assets", 0)),
                strategy_accounting_assets=str(
                    amounts.get((row["accounting_asset"], "strategy_accounted"), 0)
                ),
                assigned_weth=str(amounts.get((row["weth"], "assigned"), 0)),
                reserved_claim_assets=str(state.get("reserved_claim_assets", 0)),
            ),
            nav=NavWindow(
                report_nonce=int(state.get("last_report_nonce", 0)),
                valid_after_block=state.get("nav_valid_after_block"),
                valid_until_block=state.get("nav_valid_until_block"),
                stale=stale,
            ),
            status=self._status(state),
            actions=actions,
            as_of_block=state.get("as_of_block"),
            as_of_block_hash=state.get("as_of_block_hash"),
            indexed_at=state.get("indexed_at"),
            stale=stale,
        )

    def position(self, fund_key: str, wallet: str) -> FundPositionResponse:
        row = self._find(fund_key)
        state = self.repository.state(int(row["chain_id"]), row["fund_address"]) or {}
        position = self.repository.position(
            int(row["chain_id"]), row["fund_address"], wallet
        )
        shares = int(position.get("shares", 0))
        denominator = int(state.get("share_supply", 0)) + int(
            state.get("virtual_shares", 0)
        )
        value = (
            shares * (int(state.get("net_assets", 0)) + 1) // denominator
            if denominator
            else 0
        )
        redemption = self._redemption(position.get("redemption", {}))
        context = self._write_context(row, state)
        stale = context["stale"]
        return FundPositionResponse(
            fund_key=fund_key,
            address=wallet,
            shares=str(shares),
            accounting_value=str(value),
            redemption=redemption,
            actions=self._actions(
                row,
                state,
                common=context["reason"],
                shares=shares,
                redemption=redemption,
            ),
            as_of_block=state.get("as_of_block"),
            indexed_at=state.get("indexed_at"),
            stale=stale,
        )

    def config(self, fund_key: str) -> FundConfigResponse:
        row = self._find(fund_key)
        state = self.repository.state(int(row["chain_id"]), row["fund_address"]) or {}
        context = self._write_context(row, state)
        actions = self._actions(row, state, common=context["reason"])
        reason = actions.deposit.reason_code or actions.request_redemption.reason_code
        contracts = [
            TrustedContract(
                role=item["contract_role"],
                address=item["contract_address"],
                implementation_address=item.get("implementation_address"),
                interface_version=int(item["interface_version"]),
            )
            for item in context["active_contracts"]
        ]
        return FundConfigResponse(
            fund_key=fund_key,
            deployment_status=row["deployment_status"],
            contracts=contracts,
            capabilities=actions,
            writes_enabled=reason is None,
            blocked_reason_code=reason,
        )

    def activity(
        self, fund_key: str, cursor: str | None, limit: int
    ) -> ActivityResponse:
        row = self._find(fund_key)
        decoded = self._decode_cursor(cursor) if cursor else None
        rows = self.repository.activity(
            int(row["chain_id"]), row["fund_address"], decoded, limit + 1
        )
        visible = rows[:limit]
        items = [
            ActivityItem(
                activity_type=item["activity_type"],
                transaction_hash=item["transaction_hash"],
                block_number=int(item["block_number"]),
                log_index=int(item["log_index"]),
                wallet_address=item.get("wallet_address"),
                details=self._safe_details(item),
            )
            for item in visible
        ]
        next_cursor = self._encode_cursor(visible[-1]) if len(rows) > limit else None
        return ActivityResponse(items=items, next_cursor=next_cursor, limit=limit)

    def _find(self, fund_key: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9:_-]{0,127}", fund_key):
            raise ValueError("Invalid fund key")
        rows = [
            row
            for row in self.repository.registries()
            if row.get("enabled", False) and row["fund_key"] == fund_key
        ]
        if not rows:
            raise UnknownFundError(fund_key)
        return rows[0]

    @staticmethod
    def _registry(row: dict[str, Any]) -> FundRegistryItem:
        return FundRegistryItem(
            fund_key=row["fund_key"],
            chain_id=int(row["chain_id"]),
            fund_address=row["fund_address"],
            deployment_status=row["deployment_status"],
            share_token=TokenMetadata(
                address=row["share_token"],
                symbol=row["share_symbol"],
                decimals=int(row["share_decimals"]),
            ),
            accounting_asset=TokenMetadata(
                address=row["accounting_asset"],
                symbol=row["accounting_asset_symbol"],
                decimals=int(row["accounting_asset_decimals"]),
            ),
        )

    @staticmethod
    def _status(state: dict[str, Any]) -> FundStatus:
        return FundStatus(
            reconciled=bool(state.get("reconciled", False)),
            deposits_paused=bool(state.get("deposits_paused", True)),
            redemptions_paused=bool(state.get("redemptions_paused", True)),
            execution_locked=bool(state.get("execution_lock_owner")),
            flow_processing=bool(state.get("has_active_processing", False)),
        )

    def _actions(
        self, registry, state, *, common, shares=None, redemption=None
    ) -> FundActions:
        redemption = redemption or RedemptionView()
        deposit_reason = common or (
            "DEPOSITS_PAUSED" if state.get("deposits_paused", True) else None
        )
        redeem_reason = common or (
            "REDEMPTIONS_PAUSED" if state.get("redemptions_paused", True) else None
        )
        request_reason = redeem_reason or (
            "NO_SHARES" if shares is not None and shares == 0 else None
        )
        cancel_reason = None if common == "FLOW_PROCESSING" else common
        if cancel_reason is None and int(redemption.claimable_shares) > 0:
            cancel_reason = "CLAIMABLE_REDEMPTION_EXISTS"
        if cancel_reason is None and int(redemption.pending_shares) == 0:
            cancel_reason = "NO_PENDING_REDEMPTION"
        if cancel_reason is None and (
            redemption.latest_batch_processing
            or redemption.latest_batch_unwind_committed
        ):
            cancel_reason = "FLOW_PROCESSING"
        claim_reason = common or (
            "NO_CLAIMABLE_REDEMPTION" if int(redemption.claimable_assets) == 0 else None
        )
        return FundActions(
            deposit=self._availability(deposit_reason),
            request_redemption=self._availability(request_reason),
            cancel_redemption=self._availability(cancel_reason),
            claim_redemption=self._availability(claim_reason),
        )

    @staticmethod
    def _state_reason(registry, state, stale) -> str | None:
        if registry["deployment_status"] != "DEPLOYED":
            return "MISSING_TRUSTED_DEPLOYMENT"
        if not state.get("reconciled", False):
            return "UNRECONCILED"
        if stale:
            return "STALE_SNAPSHOT"
        if state.get("execution_lock_owner"):
            return "EXECUTION_LOCKED"
        if state.get("has_active_processing"):
            return "FLOW_PROCESSING"
        return None

    def _write_context(self, registry, state) -> dict[str, Any]:
        chain_id = int(registry["chain_id"])
        contracts = self.repository.contracts(chain_id, registry["fund_address"])
        head = self.repository.confirmed_head(chain_id)
        active = self._active_contracts(contracts, state.get("as_of_block"))
        trust_reason = self._binding_reason(registry, state, active)
        stale_reason = self._freshness_reason(state, head)
        stale = bool(trust_reason or stale_reason or state.get("nav_stale", True))
        reason = trust_reason or self._state_reason(registry, state, stale)
        if reason == "STALE_SNAPSHOT" and stale_reason:
            reason = stale_reason
        return {"reason": reason, "stale": stale, "active_contracts": active}

    @staticmethod
    def _active_contracts(contracts, as_of_block) -> list[dict[str, Any]]:
        if as_of_block is None:
            return []
        block = int(as_of_block)
        return [
            row
            for row in contracts
            if int(row["valid_from_block"]) <= block
            and (
                row.get("valid_to_block") is None or block <= int(row["valid_to_block"])
            )
        ]

    @staticmethod
    def _binding_reason(registry, state, active) -> str | None:
        if registry.get("deployment_status") != "DEPLOYED" or not state:
            return "MISSING_TRUSTED_DEPLOYMENT"
        by_role = {row["contract_role"]: row for row in active}
        if len(by_role) != len(active):
            return "AMBIGUOUS_BINDING"
        if not REQUIRED_TRUSTED_ROLES.issubset(by_role):
            return "MISSING_TRUSTED_DEPLOYMENT"
        expected_addresses = {
            "fund_vault": registry["fund_address"],
            "fund_share": registry["share_token"],
        }
        if any(
            by_role[role]["contract_address"].lower() != address.lower()
            for role, address in expected_addresses.items()
        ):
            return "UNTRUSTED_BINDING"
        if any(int(row["interface_version"]) not in {1} for row in by_role.values()):
            return "UNSUPPORTED_INTERFACE"
        if any(not by_role[role].get("implementation_address") for role in PROXY_ROLES):
            return "UNTRUSTED_IMPLEMENTATION"
        return None

    def _freshness_reason(self, state, head) -> str | None:
        if not head:
            return "UNKNOWN_CONFIRMED_HEAD"
        if (
            self._age(head.get("observed_at"))
            > settings.confirmed_head_freshness_seconds
        ):
            return "STALE_CONFIRMED_HEAD"
        if self._age(state.get("indexed_at")) > settings.fund_state_freshness_seconds:
            return "STALE_INDEXER_LEASE"
        block = int(head["block_number"])
        if state.get("as_of_block") is None or int(state["as_of_block"]) > block:
            return "INCOHERENT_CONFIRMED_HEAD"
        if int(state["as_of_block"]) == block and state.get(
            "as_of_block_hash"
        ) != head.get("block_hash"):
            return "REORGED_SNAPSHOT"
        valid_after = state.get("nav_valid_after_block")
        valid_until = state.get("nav_valid_until_block")
        if valid_after is None or block < int(valid_after):
            return "NAV_NOT_ACTIVE"
        if valid_until is None or block > int(valid_until):
            return "STALE_NAV_WINDOW"
        return None

    def _age(self, value: str | None) -> float:
        if not value:
            return float("inf")
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (self.now() - observed).total_seconds()

    @staticmethod
    def _availability(reason: str | None) -> ActionAvailability:
        return ActionAvailability(available=reason is None, reason_code=reason)

    @staticmethod
    def _redemption(row: dict[str, Any]) -> RedemptionView:
        pending = int(row.get("pending_shares", 0))
        claimable = int(row.get("claimable_assets", 0))
        next_action = "claim" if claimable else "cancel_or_wait" if pending else "none"
        return RedemptionView(
            pending_shares=str(pending),
            claimable_shares=str(row.get("claimable_shares", 0)),
            claimable_assets=str(claimable),
            status=row.get("status", "none"),
            next_action=next_action,
            latest_batch_id=int(row.get("latest_batch_id", 0)),
            latest_batch_processing=bool(row.get("latest_batch_processing", False)),
            latest_batch_unwind_committed=bool(
                row.get("latest_batch_unwind_committed", False)
            ),
        )

    @staticmethod
    def _safe_details(row: dict[str, Any]) -> dict[str, str | int | bool | None]:
        allowed = {
            "assets",
            "shares",
            "amount",
            "positionId",
            "protocolVaultId",
            "lifecycle",
            "premiumEarned",
            "collateral",
            "assignedWeth",
            "reportNonce",
        }
        return {
            key: value
            for key, value in row.get("payload", {}).items()
            if key in allowed
        }

    @staticmethod
    def _encode_cursor(row: dict[str, Any]) -> str:
        raw = json.dumps(
            [int(row["block_number"]), int(row["log_index"])], separators=(",", ":")
        )
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, int]:
        try:
            values = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )
            if len(values) != 2 or min(values) < 0:
                raise ValueError
            return int(values[0]), int(values[1])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid activity cursor") from exc


def build_fund_service() -> FundService:
    return FundService(SupabaseFundRepository())
