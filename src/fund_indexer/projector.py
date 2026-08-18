from dataclasses import dataclass, field
from typing import Any

from src.fund_indexer.models import FundEvent, ZERO_ADDRESS, integer, normalize_address
from src.meta_wheel.events import WHEEL_EVENT_NAMES
from src.meta_wheel.projector import MetaWheelProjection


CSP_LIFECYCLES = {
    0: "none",
    1: "open",
    2: "awaiting_physical_delivery",
    3: "settled_otm",
    4: "assigned",
    5: "cash_fallback",
}
COVERED_CALL_LIFECYCLES = {
    **CSP_LIFECYCLES,
    4: "called_away",
}


@dataclass(slots=True)
class FundProjection:
    fund: dict[str, Any] = field(default_factory=dict)
    balances: dict[str, int] = field(default_factory=dict)
    redemptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    positions: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    inventory: dict[tuple[str, str], int] = field(default_factory=dict)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    nav_reports: dict[int, dict[str, Any]] = field(default_factory=dict)
    activities: list[dict[str, Any]] = field(default_factory=list)
    adapters: set[str] = field(default_factory=set)
    adapter_nonces: dict[str, int] = field(default_factory=dict)
    open_redemption_batch_id: int = 1
    wheel: MetaWheelProjection | None = None

    def export(self) -> dict[str, list[dict[str, Any]]]:
        chain_id = self.fund["chain_id"]
        fund_address = self.fund["fund_address"]
        common = {"chain_id": chain_id, "fund_address": fund_address}
        exported = {
            "fund_state": [{**common, **self.fund}],
            "share_balances": [
                {**common, "wallet_address": wallet, "shares": str(shares)}
                for wallet, shares in sorted(self.balances.items())
                if shares
            ],
            "redemptions": [
                {**common, "controller_address": controller, **row}
                for controller, row in sorted(self.redemptions.items())
            ],
            "redemption_batch_states": [
                {
                    **common,
                    "controller_address": controller,
                    "latest_batch_id": row["latest_batch_id"],
                    "processing": row["latest_batch_processing"],
                    "unwind_committed": row["latest_batch_unwind_committed"],
                }
                for controller, row in sorted(self.redemptions.items())
            ],
            "positions": [
                {
                    **common,
                    "adapter_address": adapter,
                    "position_id": position_id,
                    **row,
                }
                for (adapter, position_id), row in sorted(self.positions.items())
            ],
            "inventory": [
                {
                    **common,
                    "asset_address": asset,
                    "bucket": bucket,
                    "amount": str(amount),
                }
                for (asset, bucket), amount in sorted(self.inventory.items())
                if amount
            ],
            "components": [
                {**common, "component_id": component_id, **row}
                for component_id, row in sorted(self.components.items())
            ],
            "nav_reports": [
                {**common, "report_nonce": nonce, **row}
                for nonce, row in sorted(self.nav_reports.items())
            ],
            "activities": self.activities,
        }
        if self.wheel is not None:
            exported.update(self.wheel.export())
        return exported


def project_events(
    events: list[FundEvent],
    accounting_asset: str | None = None,
    weth: str | None = None,
    *,
    strategy_kind: str = "csp",
    quote_asset: str | None = None,
    to_block: int | None = None,
) -> FundProjection:
    if not events:
        raise ValueError("Cannot project an empty event stream")
    ordered = sorted(events, key=lambda event: (event.block_number, event.log_index))
    first = ordered[0]
    projection = FundProjection(
        fund={
            "chain_id": first.chain_id,
            "fund_address": first.fund_address,
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
            "last_event_block": 0,
            "accounting_asset": normalize_address(accounting_asset)
            if accounting_asset
            else None,
            "weth": normalize_address(weth) if weth else None,
            "strategy_kind": strategy_kind,
            "quote_asset": (
                normalize_address(quote_asset) if quote_asset is not None else None
            ),
        },
        wheel=(
            MetaWheelProjection(first.chain_id, first.fund_address)
            if strategy_kind == "meta_wheel"
            else None
        ),
    )
    seen: set[tuple[int, str, int]] = set()
    nav_invalidated = False
    for event in ordered:
        if event.identity in seen:
            continue
        seen.add(event.identity)
        if event.chain_id != first.chain_id or event.fund_address != first.fund_address:
            raise ValueError("A projection stream must belong to one fund")
        _apply(projection, event)
        if event.event_name == "NavInvalidated":
            nav_invalidated = True
        elif event.event_name in {"NavCommitted", "NavWindowRestored"}:
            nav_invalidated = False
        projection.fund["last_event_block"] = event.block_number
    negative_inventory = [
        key for key, value in projection.inventory.items() if value < 0
    ]
    if negative_inventory:
        raise ValueError(f"Inventory underflow for {negative_inventory[0]}")
    _refresh_nav_staleness(
        projection,
        ordered[-1].block_number if to_block is None else to_block,
        nav_invalidated=nav_invalidated,
    )
    return projection


def _refresh_nav_staleness(
    projection: FundProjection,
    to_block: int,
    *,
    nav_invalidated: bool,
) -> None:
    if nav_invalidated:
        projection.fund["nav_stale"] = True
        return
    valid_after = projection.fund.get("nav_valid_after_block")
    valid_until = projection.fund.get("nav_valid_until_block")
    projection.fund["nav_stale"] = not (
        valid_after is not None
        and valid_until is not None
        and valid_after <= to_block <= valid_until
    )


def _apply(projection: FundProjection, event: FundEvent) -> None:
    if event.event_name == "Transfer" and event.contract_role != "fund_share":
        return
    if (
        projection.wheel is not None
        and (
            event.contract_role == "wheel_coordinator"
            or (
                event.event_name == "WheelPremiumAccrued"
                and event.contract_role == "wheel_child_lane"
            )
        )
        and projection.wheel.apply(event)
    ):
        _activity(
            projection,
            event,
            {
                "WheelLaneRegistered": "wheel_lane_registered",
                "WheelLaneStatusSet": "wheel_lane_status_updated",
                "WheelTrancheQueued": "wheel_tranche_queued",
                "WheelSiblingTrancheQueued": "wheel_sibling_tranche_queued",
                "WheelTrancheOpened": "wheel_tranche_opened",
                "WheelTrancheSettlementAdvanced": "wheel_settlement_advanced",
                "WheelChildHandoff": "wheel_handoff_completed",
                "WheelAssignmentLotCreated": "wheel_assignment_created",
                "WheelCoveredCallFloorEnforced": "wheel_call_floor_enforced",
                "WheelLotStatusChanged": "wheel_assignment_status_updated",
                "WheelRedemptionReserveChanged": "wheel_redemption_reserve_updated",
                "WheelAccountingAssetsReturned": "wheel_assets_returned",
                "WheelAllocationPauseSet": "wheel_pause_updated",
                "WheelPolicyHashSet": "wheel_policy_updated",
                "WheelFloorBufferSet": "wheel_floor_buffer_updated",
                "WheelPremiumAccrued": "wheel_premium_accrued",
            }[event.event_name],
        )
        return
    if event.event_name in WHEEL_EVENT_NAMES:
        return
    if (
        projection.wheel is not None
        and event.event_name == "Upgraded"
        and event.contract_role == "wheel_coordinator"
    ):
        projection.wheel.record_upgrade(event)
        _activity(projection, event, "wheel_upgraded")
        return
    handlers = {
        "Transfer": _transfer,
        "Deposit": _deposit,
        "Withdraw": _activity_only("claim"),
        "NavCommitted": _nav_committed,
        "NavInvalidated": _nav_invalidated,
        "NavWindowRestored": _nav_restored,
        "NavSubmitted": _nav_submitted,
        "ReporterSetUpdated": _reporter_set_updated,
        "FeeConfigUpdated": _fee_config_updated,
        "ComponentUpdated": _component_updated,
        "ComponentStateUpdated": _component_state_updated,
        "RedeemRequest": _redeem_requested,
        "PendingCancelled": _pending_cancelled,
        "ClaimReserved": _claim_reserved,
        "ClaimConsumed": _claim_consumed,
        "RedeemBatchSealed": _redeem_batch_sealed,
        "RedeemBatchStarted": _redeem_batch_started,
        "RedeemBatchProcessed": _redeem_batch_processed,
        "PositionOpened": _position_opened,
        "PositionTransitioned": _position_transitioned,
        "StrategyAllocated": _strategy_allocated,
        "StrategyDeallocated": _strategy_deallocated,
        "StrategyDeallocatedInKind": _strategy_nonce_only,
        "StrategyEmergencyExited": _strategy_nonce_only,
        "AccountingAssetsReturned": _assets_returned,
        "AssignedWethSwapped": _weth_swapped,
        "UsdcNormalized": _usdc_normalized,
        "RawAssetsRecovered": _assets_recovered,
        "PhysicalDelivery": _physical_delivery,
        "PhysicalDeliveryReserved": _adapter_activity("physical_delivery_reserved"),
        "PhysicalDeliveryReleased": _adapter_activity("physical_delivery_released"),
        "PhysicalDeliverySettled": _adapter_activity("physical_delivery_settled"),
    }
    handler = handlers.get(event.event_name)
    if handler is not None:
        handler(projection, event)


def _activity_only(kind: str):
    def handler(projection: FundProjection, event: FundEvent) -> None:
        _activity(projection, event, kind)

    return handler


def _activity(projection: FundProjection, event: FundEvent, kind: str) -> None:
    projection.activities.append(
        {
            "chain_id": event.chain_id,
            "fund_address": event.fund_address,
            "transaction_hash": event.transaction_hash,
            "log_index": event.log_index,
            "block_number": event.block_number,
            "activity_type": kind,
            "wallet_address": _activity_wallet(event.args),
            "payload": event.args,
        }
    )


def _activity_wallet(args: dict[str, Any]) -> str | None:
    for key in ("controller", "owner", "user", "sender", "receiver"):
        value = args.get(key)
        if isinstance(value, str) and value.startswith("0x"):
            return normalize_address(value)
    return None


def _transfer(projection: FundProjection, event: FundEvent) -> None:
    source = normalize_address(event.args["from"])
    target = normalize_address(event.args["to"])
    value = integer(event.args["value"])
    if source != ZERO_ADDRESS:
        projection.balances[source] = projection.balances.get(source, 0) - value
        if projection.balances[source] < 0:
            raise ValueError(f"Share balance underflow for {source}")
    else:
        projection.fund["share_supply"] = str(
            integer(projection.fund["share_supply"]) + value
        )
    if target != ZERO_ADDRESS:
        projection.balances[target] = projection.balances.get(target, 0) + value
    else:
        supply = integer(projection.fund["share_supply"]) - value
        if supply < 0:
            raise ValueError("Share supply underflow")
        projection.fund["share_supply"] = str(supply)


def _nav_committed(projection: FundProjection, event: FundEvent) -> None:
    args = event.args
    nonce = integer(args["reportNonce"])
    projection.fund.update(
        net_assets=str(integer(args["netAssets"])),
        last_report_nonce=nonce,
        nav_valid_after_block=integer(args["validAfterBlock"]),
        nav_valid_until_block=integer(args["validUntilBlock"]),
    )
    projection.nav_reports[nonce] = {
        "net_assets": str(integer(args["netAssets"])),
        "valid_after_block": integer(args["validAfterBlock"]),
        "valid_until_block": integer(args["validUntilBlock"]),
        "report_hash": None,
        "fee_shares": "0",
        "block_number": event.block_number,
    }
    _activity(projection, event, "nav_refreshed")


def _deposit(projection: FundProjection, event: FundEvent) -> None:
    projection.fund["net_assets"] = str(
        integer(projection.fund["net_assets"]) + integer(event.args["assets"])
    )
    _activity(projection, event, "deposit")


def _nav_invalidated(projection: FundProjection, event: FundEvent) -> None:
    projection.fund["nav_stale"] = True
    _activity(projection, event, "nav_invalidated")


def _nav_restored(projection: FundProjection, event: FundEvent) -> None:
    projection.fund["nav_stale"] = False
    projection.fund["last_report_nonce"] = integer(event.args["reportNonce"])
    projection.fund["nav_valid_until_block"] = integer(event.args["validUntilBlock"])


def _nav_submitted(projection: FundProjection, event: FundEvent) -> None:
    nonce = integer(event.args["reportNonce"])
    report = projection.nav_reports.setdefault(
        nonce,
        {
            "net_assets": str(integer(event.args["netAssets"])),
            "valid_after_block": None,
            "valid_until_block": None,
            "block_number": event.block_number,
        },
    )
    report["report_hash"] = event.args["reportHash"]
    report["fee_shares"] = str(integer(event.args["feeShares"]))


def _reporter_set_updated(projection: FundProjection, event: FundEvent) -> None:
    projection.fund.update(
        reporter_set_version=integer(event.args["version"]),
        reporter_threshold=integer(event.args["threshold"]),
        active_reporter_count=len(event.args["reporters"]),
        active_reporters=[
            normalize_address(reporter) for reporter in event.args["reporters"]
        ],
    )


def _fee_config_updated(projection: FundProjection, event: FundEvent) -> None:
    projection.fund.update(
        fee_recipient=normalize_address(event.args["recipient"]),
        management_fee_wad=str(integer(event.args["managementFeeWad"])),
        performance_fee_bps=integer(event.args["performanceFeeBps"]),
    )


def _component_updated(projection: FundProjection, event: FundEvent) -> None:
    args = event.args
    component = projection.components.setdefault(
        args["componentId"], {"nonce": 0, "position_state_hash": None}
    )
    component.update(
        valuator_address=normalize_address(args["valuator"]),
        interface_version=integer(args["interfaceVersion"]),
        active=bool(args["active"]),
        last_event_block=event.block_number,
    )


def _component_state_updated(projection: FundProjection, event: FundEvent) -> None:
    args = event.args
    component = projection.components.setdefault(
        args["componentId"],
        {
            "valuator_address": ZERO_ADDRESS,
            "interface_version": 0,
            "active": True,
        },
    )
    component.update(
        nonce=integer(args["nonce"]),
        position_state_hash=args["positionStateHash"],
        last_event_block=event.block_number,
    )


def _redemption(projection: FundProjection, controller: str) -> dict[str, Any]:
    return projection.redemptions.setdefault(
        normalize_address(controller),
        {
            "pending_shares": 0,
            "claimable_shares": 0,
            "claimable_assets": 0,
            "status": "none",
            "last_event_block": 0,
            "latest_batch_id": 0,
            "latest_batch_processing": False,
            "latest_batch_unwind_committed": False,
        },
    )


def _redemption_status(row: dict[str, Any], terminal: str) -> str:
    if row["claimable_shares"]:
        return "claimable"
    if row["pending_shares"]:
        return "pending"
    return terminal


def _redeem_requested(projection: FundProjection, event: FundEvent) -> None:
    row = _redemption(projection, event.args["controller"])
    row["pending_shares"] += integer(event.args["shares"])
    row["latest_batch_id"] = projection.open_redemption_batch_id
    row["status"] = _redemption_status(row, "pending")
    row["last_event_block"] = event.block_number
    _activity(projection, event, "redemption_requested")


def _pending_cancelled(projection: FundProjection, event: FundEvent) -> None:
    row = _redemption(projection, event.args["controller"])
    row["pending_shares"] -= integer(event.args["shares"])
    if row["pending_shares"] < 0:
        raise ValueError("Pending redemption share underflow")
    if row["pending_shares"] == 0:
        _clear_latest_batch(row)
    row["status"] = _redemption_status(row, "cancelled")
    row["last_event_block"] = event.block_number
    _activity(projection, event, "redemption_cancelled")


def _claim_reserved(projection: FundProjection, event: FundEvent) -> None:
    row = _redemption(projection, event.args["controller"])
    shares = integer(event.args["shares"])
    assets = integer(event.args["assets"])
    row["pending_shares"] -= shares
    if row["pending_shares"] < 0:
        raise ValueError("Pending redemption share underflow")
    row["claimable_shares"] += shares
    row["claimable_assets"] += assets
    if row["pending_shares"] == 0:
        _clear_latest_batch(row)
    row["status"] = _redemption_status(row, "claimable")
    row["last_event_block"] = event.block_number
    projection.fund["reserved_claim_assets"] = str(
        integer(projection.fund["reserved_claim_assets"]) + assets
    )
    net_assets = integer(projection.fund["net_assets"]) - assets
    if net_assets < 0:
        raise ValueError("Fund net assets underflow")
    projection.fund["net_assets"] = str(net_assets)
    _activity(projection, event, "redemption_claimable")


def _claim_consumed(projection: FundProjection, event: FundEvent) -> None:
    row = _redemption(projection, event.args["controller"])
    shares = integer(event.args["shares"])
    assets = integer(event.args["assets"])
    row["claimable_shares"] -= shares
    row["claimable_assets"] -= assets
    reserved = integer(projection.fund["reserved_claim_assets"]) - assets
    if min(row["claimable_shares"], row["claimable_assets"], reserved) < 0:
        raise ValueError("Claimable redemption underflow")
    projection.fund["reserved_claim_assets"] = str(reserved)
    row["status"] = _redemption_status(row, "claimed")
    row["last_event_block"] = event.block_number
    _activity(projection, event, "redemption_claimed")


def _redeem_batch_sealed(projection: FundProjection, event: FundEvent) -> None:
    batch_id = integer(event.args["batchId"])
    projection.open_redemption_batch_id = batch_id + 1


def _redeem_batch_started(projection: FundProjection, event: FundEvent) -> None:
    _set_latest_batch_state(projection, integer(event.args["batchId"]), True)
    _activity(projection, event, "redemption_processing")


def _redeem_batch_processed(projection: FundProjection, event: FundEvent) -> None:
    if event.args["roundComplete"]:
        _set_latest_batch_state(projection, integer(event.args["batchId"]), False)
    _activity(projection, event, "redemption_processed")


def _set_latest_batch_state(
    projection: FundProjection, batch_id: int, active: bool
) -> None:
    for row in projection.redemptions.values():
        if row["latest_batch_id"] == batch_id:
            row["latest_batch_processing"] = active
            row["latest_batch_unwind_committed"] = active


def _clear_latest_batch(row: dict[str, Any]) -> None:
    row["latest_batch_id"] = 0
    row["latest_batch_processing"] = False
    row["latest_batch_unwind_committed"] = False


def _position_opened(projection: FundProjection, event: FundEvent) -> None:
    args = event.args
    position_id = integer(args["positionId"])
    adapter = normalize_address(event.contract_address)
    projection.adapters.add(adapter)
    projection.positions[(adapter, position_id)] = {
        "protocol_vault_id": integer(args["protocolVaultId"]),
        "otoken_address": normalize_address(args["oToken"]),
        "market_maker_address": normalize_address(args["marketMaker"]),
        "option_amount": str(integer(args["optionAmount"])),
        "collateral": str(integer(args["collateral"])),
        "premium_earned": str(integer(args["premiumEarned"])),
        "collateral_returned": "0",
        "settlement_payout": "0",
        "payment": "0",
        "assigned_weth": "0",
        "called_away_usdc": "0",
        "fallback_weth_recovered": "0",
        "mm_weth_payout": "0",
        "lifecycle": "open",
        "lifecycle_hash": args["lifecycleHash"],
        "opened_block": event.block_number,
        "settled_block": None,
        "strike_price_8": (
            str(integer(args["strikePrice8"]))
            if args.get("strikePrice8") is not None
            else None
        ),
        "expiry_timestamp": (
            integer(args["expiryTimestamp"])
            if args.get("expiryTimestamp") is not None
            else None
        ),
        "is_put": (bool(args["isPut"]) if args.get("isPut") is not None else None),
        "strategy_kind": projection.fund.get("strategy_kind", "csp"),
    }
    accounting_asset = projection.fund.get("accounting_asset")
    strategy_kind = projection.fund.get("strategy_kind", "csp")
    if accounting_asset and strategy_kind == "csp":
        adapter_delta = integer(args["premiumEarned"]) - integer(args["collateral"])
        _inventory_add(
            projection, accounting_asset, "strategy_accounted", adapter_delta
        )
    elif accounting_asset and strategy_kind == "covered_call":
        quote_asset = projection.fund.get("quote_asset")
        _inventory_add(
            projection,
            accounting_asset,
            "strategy_accounted",
            -integer(args["collateral"]),
        )
        if quote_asset:
            _inventory_add(
                projection,
                quote_asset,
                "transient_usdc",
                integer(args["premiumEarned"]),
            )
    _activity(
        projection,
        event,
        "covered_call_opened" if strategy_kind == "covered_call" else "csp_opened",
    )


def _position_transitioned(projection: FundProjection, event: FundEvent) -> None:
    args = event.args
    position_id = integer(args["positionId"])
    adapter = normalize_address(event.contract_address)
    position = projection.positions.get((adapter, position_id))
    if position is None:
        raise ValueError(f"Transition for unknown fund position {position_id}")
    strategy_kind = projection.fund.get("strategy_kind", "csp")
    lifecycle_map = (
        COVERED_CALL_LIFECYCLES if strategy_kind == "covered_call" else CSP_LIFECYCLES
    )
    lifecycle = lifecycle_map[integer(args["lifecycle"])]
    if strategy_kind == "covered_call":
        _covered_call_transition(projection, event, position, lifecycle)
        return
    collateral_delta = integer(args["collateralDelta"])
    payment = integer(args["payment"])
    weth_delta = integer(args["wethDelta"])
    collateral_returned = integer(position["collateral_returned"])
    settlement_payout = integer(position["settlement_payout"])
    if lifecycle in {"settled_otm", "awaiting_physical_delivery"}:
        collateral_returned += collateral_delta
    elif lifecycle == "cash_fallback":
        settlement_payout += collateral_delta
    position.update(
        lifecycle=lifecycle,
        collateral_returned=str(collateral_returned),
        settlement_payout=str(settlement_payout),
        payment=str(integer(position["payment"]) + payment),
        assigned_weth=str(integer(position["assigned_weth"]) + weth_delta),
        lifecycle_hash=args["lifecycleHash"],
    )
    accounting_asset = projection.fund.get("accounting_asset")
    weth = projection.fund.get("weth")
    if accounting_asset and collateral_delta >= payment:
        _inventory_add(
            projection,
            accounting_asset,
            "strategy_accounted",
            collateral_delta - payment,
        )
    if weth and weth_delta:
        _inventory_add(projection, weth, "assigned", weth_delta)
    if lifecycle not in {"open", "awaiting_physical_delivery"}:
        position["settled_block"] = event.block_number
    activity = {
        "settled_otm": "csp_settled_otm",
        "assigned": "csp_assigned",
        "cash_fallback": "csp_cash_fallback",
    }.get(lifecycle, "csp_awaiting_delivery")
    _activity(projection, event, activity)


def _covered_call_transition(
    projection: FundProjection,
    event: FundEvent,
    position: dict[str, Any],
    lifecycle: str,
) -> None:
    args = event.args
    weth_delta = integer(args["collateralDelta"])
    usdc_delta = integer(args["payment"])
    mm_weth_payout = integer(args["wethDelta"])
    collateral_returned = integer(position["collateral_returned"])
    fallback_recovered = integer(position["fallback_weth_recovered"])
    if lifecycle == "settled_otm":
        collateral_returned += weth_delta
    elif lifecycle == "cash_fallback":
        fallback_recovered += weth_delta
    position.update(
        lifecycle=lifecycle,
        collateral_returned=str(collateral_returned),
        called_away_usdc=str(integer(position["called_away_usdc"]) + usdc_delta),
        fallback_weth_recovered=str(fallback_recovered),
        mm_weth_payout=str(integer(position["mm_weth_payout"]) + mm_weth_payout),
        lifecycle_hash=args["lifecycleHash"],
    )
    accounting_asset = projection.fund.get("accounting_asset")
    quote_asset = projection.fund.get("quote_asset")
    if accounting_asset and weth_delta:
        _inventory_add(projection, accounting_asset, "strategy_accounted", weth_delta)
    if quote_asset and usdc_delta:
        _inventory_add(projection, quote_asset, "transient_usdc", usdc_delta)
    if lifecycle not in {"open", "awaiting_physical_delivery"}:
        position["settled_block"] = event.block_number
    activity = {
        "settled_otm": "covered_call_settled_otm",
        "called_away": "covered_call_called_away",
        "cash_fallback": "covered_call_cash_fallback",
    }.get(lifecycle, "covered_call_awaiting_delivery")
    _activity(projection, event, activity)


def _inventory_add(
    projection: FundProjection, asset: str, bucket: str, delta: int
) -> None:
    key = normalize_address(asset), bucket
    value = projection.inventory.get(key, 0) + delta
    projection.inventory[key] = value


def _strategy_allocated(projection: FundProjection, event: FundEvent) -> None:
    _record_strategy_nonce(projection, event)
    _inventory_add(
        projection,
        event.args["asset"],
        "strategy_accounted",
        integer(event.args["amount"]),
    )
    _activity(projection, event, "strategy_allocated")


def _strategy_deallocated(projection: FundProjection, event: FundEvent) -> None:
    _record_strategy_nonce(projection, event)
    _activity(projection, event, "strategy_deallocated")


def _strategy_nonce_only(projection: FundProjection, event: FundEvent) -> None:
    _record_strategy_nonce(projection, event)


def _record_strategy_nonce(projection: FundProjection, event: FundEvent) -> None:
    adapter = normalize_address(event.args["adapter"])
    projection.adapters.add(adapter)
    projection.adapter_nonces[adapter] = integer(event.args["positionNonce"])


def _assets_returned(projection: FundProjection, event: FundEvent) -> None:
    asset = projection.fund.get("accounting_asset")
    if asset:
        _inventory_add(
            projection, asset, "strategy_accounted", -integer(event.args["amount"])
        )


def _weth_swapped(projection: FundProjection, event: FundEvent) -> None:
    weth = projection.fund.get("weth")
    usdc = projection.fund.get("accounting_asset")
    if weth and usdc:
        _inventory_add(projection, weth, "assigned", -integer(event.args["wethIn"]))
        _inventory_add(
            projection, usdc, "strategy_accounted", integer(event.args["usdcOut"])
        )


def _usdc_normalized(projection: FundProjection, event: FundEvent) -> None:
    if projection.fund.get("strategy_kind") != "covered_call":
        return
    accounting_asset = projection.fund.get("accounting_asset")
    quote_asset = projection.fund.get("quote_asset")
    if accounting_asset and quote_asset:
        _inventory_add(
            projection,
            quote_asset,
            "transient_usdc",
            -integer(event.args["usdcIn"]),
        )
        _inventory_add(
            projection,
            accounting_asset,
            "strategy_accounted",
            integer(event.args["wethOut"]),
        )
    _activity(projection, event, "covered_call_usdc_normalized")


def _assets_recovered(projection: FundProjection, event: FundEvent) -> None:
    for asset, amount in zip(event.args["assets"], event.args["amounts"], strict=True):
        normalized = normalize_address(asset)
        if projection.fund.get("strategy_kind") == "covered_call":
            bucket = (
                "strategy_accounted"
                if normalized == projection.fund.get("accounting_asset")
                else "transient_usdc"
            )
        else:
            bucket = (
                "assigned"
                if normalized == projection.fund.get("weth")
                else "strategy_accounted"
            )
        _inventory_add(projection, asset, bucket, -integer(amount))


def _physical_delivery(projection: FundProjection, event: FundEvent) -> None:
    if normalize_address(event.args["user"]) not in projection.adapters:
        return
    _activity(projection, event, "physical_delivery")


def _adapter_activity(kind: str):
    def handler(projection: FundProjection, event: FundEvent) -> None:
        if normalize_address(event.args["owner"]) in projection.adapters:
            _activity(projection, event, kind)

    return handler
