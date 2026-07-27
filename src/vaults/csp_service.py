"""DB-first product service for tokenized option funds."""

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
    CspPositionSummary,
    FundActions,
    FundComposition,
    FundConfigResponse,
    FundListResponse,
    FundPositionResponse,
    FundRegistryItem,
    FundStatus,
    FundStrategySnapshot,
    FundSummaryResponse,
    NavWindow,
    RedemptionView,
    StrategyOperationSummary,
    StressNav,
    TokenMetadata,
    TrustedContract,
)

COMMON_PROXY_ROLES = {
    "fund_vault",
    "fund_share",
    "fund_accounting",
    "fund_flow_manager",
    "strategy_manager",
    "controller",
    "batch_settler",
}
STRATEGY_PROXY_ROLES = {"csp_adapter", "covered_call_adapter"}
PROXY_ROLES = COMMON_PROXY_ROLES | STRATEGY_PROXY_ROLES
COMMON_TRUSTED_ROLES = COMMON_PROXY_ROLES | {
    "claim_escrow",
    "access_manager",
    "address_book",
    "margin_pool",
    "nav_verifier",
    "oracle",
    "otoken_factory",
    "swap_router",
    "whitelist",
}
REQUIRED_TRUSTED_ROLES = COMMON_TRUSTED_ROLES | {
    "csp_adapter",
    "csp_valuator",
}


def required_trusted_roles(strategy_kind: str) -> set[str]:
    if strategy_kind == "covered_call":
        return COMMON_TRUSTED_ROLES | {
            "covered_call_adapter",
            "covered_call_valuator",
        }
    return REQUIRED_TRUSTED_ROLES


class UnknownFundError(LookupError):
    """The requested fund key is not registered."""


class FundRepository(Protocol):
    def registries(self) -> list[dict[str, Any]]: ...
    def state(self, chain_id: int, fund: str) -> dict[str, Any] | None: ...
    def inventory(self, chain_id: int, fund: str) -> list[dict[str, Any]]: ...
    def positions(self, chain_id: int, fund: str) -> list[dict[str, Any]]: ...
    def nav_valuation(
        self, chain_id: int, fund: str, report_nonce: int
    ) -> dict[str, Any] | None: ...
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

    def positions(self, chain_id: int, fund: str) -> list[dict[str, Any]]:
        return (
            self._fund_query("v2_fund_strategy_positions", chain_id, fund)
            .order("position_id")
            .execute()
            .data
            or []
        )

    def nav_valuation(
        self, chain_id: int, fund: str, report_nonce: int
    ) -> dict[str, Any] | None:
        if report_nonce <= 0:
            return None
        run = (
            self._fund_query("v2_nav_report_runs", chain_id, fund)
            .eq("report_nonce", report_nonce)
            .eq("status", "confirmed")
            .limit(1)
            .execute()
        )
        if not run.data:
            return None
        row = run.data[0]
        snapshot_block = int(row["snapshot_block"])
        marks = (
            self._fund_query("v2_csp_fair_value_marks", chain_id, fund)
            .eq("snapshot_block", snapshot_block)
            .order("position_id")
            .execute()
            .data
            or []
        )
        return {
            "snapshot_block": snapshot_block,
            "snapshot_block_hash": row["snapshot_block_hash"],
            "reports": row.get("reports") or [],
            "marks": marks,
        }

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
        positions = self.repository.positions(int(row["chain_id"]), row["fund_address"])
        active_positions = [
            position
            for position in positions
            if position.get("lifecycle") in {"open", "awaiting_physical_delivery"}
        ]
        valuation = self.repository.nav_valuation(
            int(row["chain_id"]),
            row["fund_address"],
            int(state.get("last_report_nonce", 0)),
        )
        context = self._write_context(row, state, positions=positions)
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
        adapter_free = int(
            amounts.get((row["accounting_asset"], "strategy_accounted"), 0)
        )
        strategy_kind = row.get("strategy_kind", "csp")
        assigned_weth = (
            int(amounts.get((row["weth"], "assigned"), 0))
            if strategy_kind == "csp"
            else 0
        )
        transient_usdc = (
            int(
                amounts.get(
                    (row.get("quote_asset"), "transient_usdc"),
                    0,
                )
            )
            if strategy_kind == "covered_call" and row.get("quote_asset")
            else 0
        )
        locked_collateral = sum(
            int(position.get("collateral", 0))
            for position in active_positions
            if position.get("lifecycle") == "open"
        )
        valuation_view = self._valuation_view(
            valuation=valuation,
            idle_assets=int(state.get("accounted_idle_assets", 0)),
            adapter_free_assets=adapter_free,
            locked_collateral_assets=locked_collateral,
            assigned_weth=assigned_weth,
            transient_usdc=transient_usdc,
            strategy_kind=strategy_kind,
            normalization_slippage_bps=int(state.get("normalization_slippage_bps", 0)),
            denominator=denominator,
            share_decimals=int(row["share_decimals"]),
        )
        return FundSummaryResponse(
            fund=self._registry(row),
            net_assets=str(net_assets),
            share_supply=str(supply),
            virtual_shares=str(virtual),
            share_price_assets=str(share_price),
            stress_price_assets=valuation_view["stress_price_assets"],
            composition=FundComposition(
                idle_assets=str(state.get("accounted_idle_assets", 0)),
                strategy_accounting_assets=str(adapter_free),
                assigned_weth=str(assigned_weth),
                reserved_claim_assets=str(state.get("reserved_claim_assets", 0)),
                gross_assets=str(valuation_view["gross_assets"]),
                adapter_free_accounting_assets=str(adapter_free),
                locked_collateral_assets=str(locked_collateral),
                fair_option_liability_assets=str(
                    valuation_view["fair_liability_assets"]
                ),
                assigned_weth_value_assets=str(
                    valuation_view["assigned_weth_value_assets"]
                ),
                settlement_receivable_assets=str(
                    valuation_view["settlement_receivable_assets"]
                ),
                settlement_cost_assets=str(valuation_view["settlement_cost_assets"]),
                transient_usdc=str(transient_usdc),
                transient_usdc_value_assets=str(
                    valuation_view["transient_usdc_value_assets"]
                ),
                normalization_cost_assets=str(
                    valuation_view["normalization_cost_assets"]
                ),
                option_exit_cost_assets=str(valuation_view["option_exit_cost_assets"]),
            ),
            nav=NavWindow(
                report_nonce=int(state.get("last_report_nonce", 0)),
                valid_after_block=state.get("nav_valid_after_block"),
                valid_until_block=state.get("nav_valid_until_block"),
                stale=stale,
                methodology=valuation_view["methodology"],
                model_version=valuation_view["model_version"],
                observed_at=valuation_view["observed_at"],
                source_quality=valuation_view["source_quality"],
                stress=valuation_view["stress"],
            ),
            strategy=self._strategy_snapshot(
                positions,
                valuation,
                strategy_kind=strategy_kind,
                transient_usdc=transient_usdc,
            ),
            status=self._status(state),
            actions=actions,
            as_of_block=state.get("as_of_block"),
            as_of_block_hash=state.get("as_of_block_hash"),
            indexed_at=state.get("indexed_at"),
            stale=stale,
        )

    @staticmethod
    def _strategy_snapshot(
        positions: list[dict[str, Any]],
        valuation: dict[str, Any] | None,
        *,
        strategy_kind: str = "csp",
        transient_usdc: int = 0,
    ) -> FundStrategySnapshot:
        total_premium = sum(
            max(int(position.get("premium_earned", 0)), 0) for position in positions
        )
        active = [
            position
            for position in positions
            if position.get("lifecycle") in {"open", "awaiting_physical_delivery"}
        ]
        selected = max(
            active or positions,
            key=lambda position: int(position.get("position_id", 0)),
            default=None,
        )
        if selected is None:
            return FundStrategySnapshot(
                strategy_kind=strategy_kind,
                total_premium_collected_assets=str(total_premium),
                next_open_condition="when_funded_and_pricing_is_ready",
            )

        position_id = int(selected.get("position_id", 0))
        marks = (valuation or {}).get("marks") or []
        mark = next(
            (item for item in marks if int(item.get("position_id", -1)) == position_id),
            None,
        )
        is_active = selected in active
        expiry = (
            int(mark["expiry_timestamp"])
            if mark
            else (
                int(selected["expiry_timestamp"])
                if selected.get("expiry_timestamp") is not None
                else None
            )
        )
        strike = (
            str(mark["strike_price_8"])
            if mark is not None
            else (
                str(selected["strike_price_8"])
                if selected.get("strike_price_8") is not None
                else None
            )
        )
        lifecycle = str(selected.get("lifecycle", "unknown"))
        operation = {
            "open": "call_opened" if strategy_kind == "covered_call" else "put_opened",
            "awaiting_physical_delivery": "awaiting_physical_delivery",
            "settled_otm": (
                "call_settled_otm"
                if strategy_kind == "covered_call"
                else "put_settled_otm"
            ),
            "called_away": "call_called_away",
            "assigned": "put_assigned",
            "cash_fallback": "cash_fallback",
        }.get(lifecycle, "position_updated")
        next_condition = "when_pricing_is_ready"
        if lifecycle == "awaiting_physical_delivery":
            next_condition = "awaiting_physical_delivery"
        elif is_active:
            next_condition = "after_current_settlement"
        elif strategy_kind == "covered_call" and transient_usdc:
            next_condition = "after_usdc_normalization"
        return FundStrategySnapshot(
            strategy_kind=strategy_kind,
            latest_position=CspPositionSummary(
                position_id=position_id,
                lifecycle=lifecycle,
                strike_price_usd_8=strike,
                expiry_timestamp=expiry,
                option_amount_8=str(selected.get("option_amount", 0)),
                collateral_assets=str(selected.get("collateral", 0)),
                premium_earned_assets=str(selected.get("premium_earned", 0)),
                called_away_usdc=str(selected.get("called_away_usdc", 0)),
                fallback_weth_recovered_assets=str(
                    selected.get("fallback_weth_recovered", 0)
                ),
                mm_weth_payout_assets=str(selected.get("mm_weth_payout", 0)),
            ),
            latest_operation=StrategyOperationSummary(
                operation_type=operation,
                position_id=position_id,
                block_number=(
                    int(selected["settled_block"])
                    if selected.get("settled_block") is not None
                    else (
                        int(selected["opened_block"])
                        if selected.get("opened_block") is not None
                        else None
                    )
                ),
            ),
            total_premium_collected_assets=str(total_premium),
            next_open_after=expiry if is_active else None,
            next_open_condition=next_condition,
        )

    @staticmethod
    def _valuation_view(
        *,
        valuation,
        idle_assets,
        adapter_free_assets,
        locked_collateral_assets,
        assigned_weth,
        transient_usdc,
        strategy_kind,
        normalization_slippage_bps,
        denominator,
        share_decimals,
    ) -> dict[str, Any]:
        fallback_gross = idle_assets + adapter_free_assets + locked_collateral_assets
        fallback = {
            "gross_assets": fallback_gross,
            "fair_liability_assets": 0,
            "assigned_weth_value_assets": 0,
            "settlement_receivable_assets": 0,
            "settlement_cost_assets": 0,
            "transient_usdc_value_assets": 0,
            "normalization_cost_assets": 0,
            "option_exit_cost_assets": 0,
            "stress_price_assets": None,
            "methodology": None,
            "model_version": None,
            "observed_at": None,
            "source_quality": None,
            "stress": None,
        }
        if not valuation:
            return fallback
        reports = valuation.get("reports") or []
        marks = valuation.get("marks") or []
        if not reports:
            return fallback
        gross_assets = sum(int(report.get("grossAssets", 0)) for report in reports)
        fair_liability = sum(int(report.get("liabilities", 0)) for report in reports)
        settlement_cost = sum(int(report.get("baseExitCost", 0)) for report in reports)
        accounted = idle_assets + adapter_free_assets + locked_collateral_assets
        non_usdc_value = max(gross_assets - accounted, 0)
        assigned_weth_value = (
            non_usdc_value if strategy_kind == "csp" and assigned_weth else 0
        )
        transient_usdc_value = (
            non_usdc_value if strategy_kind == "covered_call" and transient_usdc else 0
        )
        settlement_receivable = max(
            non_usdc_value - assigned_weth_value - transient_usdc_value,
            0,
        )
        normalization_cost = (
            (transient_usdc_value * normalization_slippage_bps + 10_000 - 1) // 10_000
            if transient_usdc_value and normalization_slippage_bps
            else 0
        )
        option_exit_cost = max(settlement_cost - normalization_cost, 0)
        stress_liability = sum(
            int(mark.get("stress_liability_assets", 0)) for mark in marks
        )
        stress_net = max(gross_assets - stress_liability - settlement_cost, 0)
        stress_price = (
            (stress_net + 1) * 10**share_decimals // denominator
            if denominator and marks
            else None
        )
        model_versions = {int(mark["model_version"]) for mark in marks}
        methodologies = {mark["methodology"] for mark in marks}
        source_qualities = {mark["source_quality"] for mark in marks}
        return {
            "gross_assets": gross_assets,
            "fair_liability_assets": fair_liability,
            "assigned_weth_value_assets": assigned_weth_value,
            "settlement_receivable_assets": settlement_receivable,
            "settlement_cost_assets": settlement_cost,
            "transient_usdc_value_assets": transient_usdc_value,
            "normalization_cost_assets": normalization_cost,
            "option_exit_cost_assets": option_exit_cost,
            "stress_price_assets": str(stress_price)
            if stress_price is not None
            else None,
            "methodology": (
                next(iter(methodologies))
                if len(methodologies) == 1
                else (
                    "signed_observer_quorum"
                    if strategy_kind == "covered_call"
                    else None
                )
            ),
            "model_version": next(iter(model_versions))
            if len(model_versions) == 1
            else None,
            "observed_at": max(
                (
                    mark["observed_at"]
                    for mark in marks
                    if mark.get("observed_at") is not None
                ),
                default=None,
            ),
            "source_quality": (
                next(iter(source_qualities))
                if len(source_qualities) == 1
                else (
                    "mixed_sources"
                    if strategy_kind == "covered_call"
                    else None
                )
            ),
            "stress": StressNav(
                net_assets=str(stress_net),
                share_price_assets=str(stress_price),
                option_liability_assets=str(stress_liability),
            )
            if stress_price is not None
            else None,
        }

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
        quote_asset = None
        if row.get("quote_asset"):
            quote_asset = TokenMetadata(
                address=row["quote_asset"],
                symbol=row.get("quote_asset_symbol") or "USDC",
                decimals=int(row.get("quote_asset_decimals", 6)),
            )
        return FundRegistryItem(
            fund_key=row["fund_key"],
            strategy_kind=row.get("strategy_kind", "csp"),
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
            quote_asset=quote_asset,
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

    def _write_context(self, registry, state, *, positions=None) -> dict[str, Any]:
        chain_id = int(registry["chain_id"])
        if positions is None:
            positions = self.repository.positions(
                chain_id, registry["fund_address"]
            )
        contracts = self.repository.contracts(chain_id, registry["fund_address"])
        head = self.repository.confirmed_head(chain_id)
        active = self._active_contracts(contracts, state.get("as_of_block"))
        trust_reason = self._binding_reason(registry, state, active)
        stale_reason = self._freshness_reason(state, head)
        settlement_reason = (
            "AWAITING_PHYSICAL_DELIVERY"
            if registry.get("strategy_kind") == "covered_call"
            and any(
                position.get("lifecycle") == "awaiting_physical_delivery"
                for position in positions
            )
            else None
        )
        stale = bool(
            trust_reason
            or stale_reason
            or settlement_reason
            or state.get("nav_stale", True)
        )
        reason = (
            trust_reason
            or settlement_reason
            or self._state_reason(registry, state, stale)
        )
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
        required = required_trusted_roles(registry.get("strategy_kind", "csp"))
        if not required.issubset(by_role):
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
        required_proxies = COMMON_PROXY_ROLES | {
            (
                "covered_call_adapter"
                if registry.get("strategy_kind") == "covered_call"
                else "csp_adapter"
            )
        }
        if any(
            not by_role[role].get("implementation_address") for role in required_proxies
        ):
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
