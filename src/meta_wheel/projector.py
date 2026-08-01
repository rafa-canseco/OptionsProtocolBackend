"""Deterministic Meta Wheel event projection and invariant checks."""

from dataclasses import dataclass, field
from typing import Any

from src.fund_indexer.models import FundEvent, integer, normalize_address


TRANCHE_LEGS = {
    1: "pending_csp",
    2: "csp_open",
    3: "csp_settling",
    4: "weth_transition",
    5: "call_open",
    6: "call_settling",
    7: "closed",
}
LANE_KINDS = {1: "csp", 2: "covered_call"}
LOT_STATUSES = {
    1: "available",
    2: "in_call",
    3: "called_away",
    4: "emergency_exited",
}
SETTLEMENT_KINDS = {
    0: "pending_delivery",
    1: "csp_otm",
    2: "csp_assigned",
    3: "call_otm",
    4: "call_away",
    5: "weth_fallback",
}
NEXT_ACTION = {
    "pending_csp": "open_csp",
    "csp_open": "wait_for_csp_expiry",
    "csp_settling": "handoff_csp",
    "weth_transition": "open_covered_call_above_floor",
    "call_open": "wait_for_call_expiry",
    "call_settling": "handoff_covered_call",
    "closed": "none",
}


@dataclass(slots=True)
class MetaWheelProjection:
    chain_id: int
    fund_address: str
    state: dict[str, Any] = field(default_factory=dict)
    lanes: dict[str, dict[str, Any]] = field(default_factory=dict)
    tranches: dict[int, dict[str, Any]] = field(default_factory=dict)
    lots: dict[int, dict[str, Any]] = field(default_factory=dict)
    handoffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    _floor_buffer_observed: bool = False
    _pending_premium_positions: dict[int, tuple[str, int]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.fund_address = normalize_address(self.fund_address)
        if not self.state:
            self.state = {
                "pending_csp_usdc": 0,
                "redemption_reserved_usdc": 0,
                "reserved_principal_usdc": 0,
                "transition_weth": 0,
                "policy_version": 0,
                "policy_hash": None,
                "execution_cost_buffer_8": 0,
                "paused": False,
                "cumulative_gross_premium": 0,
                "cumulative_protocol_fee": 0,
                "cumulative_net_premium": 0,
                "upgrade_count": 0,
                "last_upgrade_implementation": None,
                "last_event_block": 0,
            }

    def apply(self, event: FundEvent) -> bool:
        handler = {
            "WheelLaneRegistered": self._lane_registered,
            "WheelLaneRemoved": self._lane_removed,
            "WheelLaneStatusSet": self._lane_status_set,
            "WheelTrancheQueued": self._tranche_queued,
            "WheelSiblingTrancheQueued": self._sibling_tranche_queued,
            "WheelTrancheOpened": self._tranche_opened,
            "WheelTrancheSettlementAdvanced": self._settlement_advanced,
            "WheelChildHandoff": self._child_handoff,
            "WheelAssignmentLotCreated": self._assignment_lot_created,
            "WheelCoveredCallFloorEnforced": self._covered_call_floor_enforced,
            "WheelLotStatusChanged": self._lot_status_changed,
            "WheelRedemptionReserveChanged": self._redemption_reserve_changed,
            "WheelRedemptionUsdcReserved": self._redemption_usdc_reserved,
            "WheelRedemptionUsdcReleased": self._redemption_usdc_released,
            "WheelAccountingAssetsReturned": self._accounting_assets_returned,
            "WheelAllocationPauseSet": self._allocation_pause_set,
            "WheelPolicyHashSet": self._policy_hash_set,
            "WheelFloorBufferSet": self._floor_buffer_set,
            "WheelPremiumAccrued": self._premium_accrued,
        }.get(event.event_name)
        if handler is None:
            return False
        handler(event)
        self.state["last_event_block"] = event.block_number
        return True

    def export(self) -> dict[str, list[dict[str, Any]]]:
        common = {"chain_id": self.chain_id, "fund_address": self.fund_address}
        protected_floor = max(
            (
                integer(row.get("required_call_floor_8", 0))
                for row in self.tranches.values()
                if row["state"] != "closed"
            ),
            default=0,
        )
        state = {
            **common,
            **{key: _db_value(value) for key, value in self.state.items()},
            "current_phase": self.current_phase(),
            "next_action": self.next_action(),
            "active_tranche_count": sum(
                row["state"] != "closed" for row in self.tranches.values()
            ),
            "protected_assignment_floor_8": str(protected_floor),
        }
        return {
            "wheel_state": [state],
            "wheel_lanes": [
                {**common, "child_vault": child_vault, **_db_row(row)}
                for child_vault, row in sorted(self.lanes.items())
            ],
            "wheel_tranches": [
                {**common, "tranche_id": tranche_id, **_db_row(row)}
                for tranche_id, row in sorted(self.tranches.items())
            ],
            "wheel_assignment_lots": [
                {**common, "lot_id": lot_id, **_db_row(row)}
                for lot_id, row in sorted(self.lots.items())
            ],
            "wheel_handoffs": [
                {**common, "handoff_id": handoff_id, **_db_row(row)}
                for handoff_id, row in sorted(self.handoffs.items())
            ],
        }

    def current_phase(self) -> str:
        phases = {
            row["state"] for row in self.tranches.values() if row["state"] != "closed"
        }
        if not phases:
            return "idle"
        if len(phases) == 1:
            return next(iter(phases))
        return "mixed"

    def next_action(self) -> str:
        if self.state["paused"]:
            return "paused"
        active = [
            row for row in self.tranches.values() if row["state"] != "closed"
        ]
        if not active:
            return "allocate_csp_lane" if self.state["pending_csp_usdc"] else "wait"
        actions = {
            (
                "wait_for_physical_delivery"
                if row.get("settlement_kind") == "pending_delivery"
                else NEXT_ACTION[row["state"]]
            )
            for row in active
        }
        return next(iter(actions)) if len(actions) == 1 else "process_ready_tranches"

    def record_upgrade(self, event: FundEvent) -> None:
        self.state["upgrade_count"] += 1
        self.state["last_upgrade_implementation"] = normalize_address(
            event.args["implementation"]
        )
        self.state["last_event_block"] = event.block_number

    def _lane_registered(self, event: FundEvent) -> None:
        child_vault = normalize_address(event.args["lane"])
        if child_vault in self.lanes:
            raise ValueError("Meta Wheel child lane registered twice")
        self.lanes[child_vault] = {
            "registration_index": len(self.lanes),
            "lane_type": _enum(LANE_KINDS, event.args["kind"], "lane kind"),
            "active": True,
            "last_event_block": event.block_number,
        }

    def _lane_status_set(self, event: FundEvent) -> None:
        child_vault, lane = self._lane(event.args["lane"])
        active = bool(event.args["active"])
        if not active and any(
            row["child_vault"] == child_vault and row["child_shares"]
            for row in self.tranches.values()
        ):
            raise ValueError("Cannot disable an in-use Meta Wheel child lane")
        lane.update(active=active, last_event_block=event.block_number)

    def _lane_removed(self, event: FundEvent) -> None:
        child_vault, lane = self._lane(event.args["lane"])
        if lane["lane_type"] != _enum(
            LANE_KINDS, event.args["kind"], "lane kind"
        ):
            raise ValueError("Meta Wheel removed lane kind changed")
        if any(
            row["child_vault"] == child_vault and row["child_shares"]
            for row in self.tranches.values()
        ):
            raise ValueError("Cannot remove an in-use Meta Wheel child lane")
        removed_index = integer(lane["registration_index"])
        last_index = len(self.lanes) - 1
        if removed_index != last_index:
            replacement = next(
                value
                for value in self.lanes.values()
                if integer(value["registration_index"]) == last_index
            )
            replacement.update(
                registration_index=removed_index,
                last_event_block=event.block_number,
            )
        del self.lanes[child_vault]

    def _tranche_queued(self, event: FundEvent) -> None:
        args = event.args
        tranche_id = integer(args["trancheId"])
        if tranche_id in self.tranches:
            raise ValueError("Meta Wheel tranche queued twice")
        amount = integer(args["usdcAmount"])
        pending = integer(args["pendingCspUsdc"])
        if amount <= 0 or pending < amount:
            raise ValueError("Invalid Meta Wheel CSP allocation")
        self.state["pending_csp_usdc"] = pending
        self.tranches[tranche_id] = {
            "allocation_id": args["allocationId"],
            "parent_tranche_id": None,
            "child_vault": None,
            "state": "pending_csp",
            "principal_assets": amount,
            "pending_assets": amount,
            "state_nonce": 1,
            "state_hash": args["stateHash"],
            "expiry": None,
            "child_position_id": None,
            "child_execution_state_hash": None,
            "settlement_kind": None,
            "child_shares": 0,
            "assignment_lot_ids": [],
            "literal_call_floor_8": 0,
            "required_call_floor_8": 0,
            "call_strike_8": None,
            "last_event_block": event.block_number,
        }

    def _sibling_tranche_queued(self, event: FundEvent) -> None:
        args = event.args
        parent_id = integer(args["parentTrancheId"])
        parent = self._tranche(parent_id)
        if parent["state"] not in {
            "pending_csp",
            "csp_settling",
            "call_settling",
        }:
            raise ValueError("Meta Wheel sibling created from an invalid leg")
        sibling_id = integer(args["siblingTrancheId"])
        amount = integer(args["usdcAmount"])
        principal = integer(args["principalUsdc"])
        if (
            sibling_id in self.tranches
            or sibling_id == parent_id
            or amount <= 0
            or principal < 0
            or (
                parent["state"] == "pending_csp"
                and amount > integer(self.state["pending_csp_usdc"])
            )
            or principal > integer(parent["principal_assets"])
        ):
            raise ValueError("Invalid Meta Wheel sibling tranche")
        if parent["state"] == "pending_csp":
            if amount >= integer(parent["pending_assets"]):
                raise ValueError("Meta Wheel split must leave parent CSP assets")
            parent["pending_assets"] -= amount
            parent["state_nonce"] = integer(parent["state_nonce"]) + 1
            parent["state_hash"] = None
        else:
            parent["pending_assets"] = 0
        parent["principal_assets"] -= principal
        parent["last_event_block"] = event.block_number
        self.tranches[sibling_id] = {
            "allocation_id": None,
            "parent_tranche_id": parent_id,
            "child_vault": None,
            "state": "pending_csp",
            "principal_assets": principal,
            "pending_assets": amount,
            "state_nonce": 1,
            "state_hash": args["stateHash"],
            "expiry": None,
            "child_position_id": None,
            "child_execution_state_hash": None,
            "settlement_kind": None,
            "child_shares": 0,
            "assignment_lot_ids": [],
            "literal_call_floor_8": 0,
            "required_call_floor_8": 0,
            "call_strike_8": None,
            "last_event_block": event.block_number,
        }

    def _tranche_opened(self, event: FundEvent) -> None:
        args = event.args
        tranche = self._tranche(args["trancheId"])
        child_vault, lane = self._lane(args["lane"])
        if not lane["active"]:
            raise ValueError("Meta Wheel tranche opened in an inactive child lane")
        leg = _enum(TRANCHE_LEGS, args["leg"], "tranche leg")
        expected = (
            ("pending_csp", "csp", "csp_open")
            if leg == "csp_open"
            else ("weth_transition", "covered_call", "call_open")
            if leg == "call_open"
            else None
        )
        if expected is None or tranche["state"] != expected[0] or lane["lane_type"] != expected[1]:
            raise ValueError("Invalid Meta Wheel child-lane open transition")
        shares = integer(args["childShares"])
        if shares <= 0:
            raise ValueError("Meta Wheel child shares must be positive")
        if leg == "csp_open":
            pending = integer(tranche["pending_assets"])
            if pending > integer(self.state["pending_csp_usdc"]):
                raise ValueError("Meta Wheel pending CSP accounting underflow")
            self.state["pending_csp_usdc"] -= pending
            tranche["pending_assets"] = 0
        else:
            lot = self._lot(self._single_lot_id(tranche))
            amount = integer(lot["remaining_weth"])
            if amount > integer(self.state["transition_weth"]):
                raise ValueError("Meta Wheel transition WETH accounting underflow")
            self.state["transition_weth"] -= amount
        pending_premium = self._pending_premium_positions.pop(
            integer(args["trancheId"]), None
        )
        if pending_premium is not None and pending_premium != (
            child_vault,
            integer(args["childPositionId"]),
        ):
            raise ValueError("Meta Wheel premium/open position binding changed")
        tranche.update(
            child_vault=child_vault,
            state=leg,
            state_nonce=integer(tranche["state_nonce"]) + 1,
            state_hash=None,
            expiry=integer(args["expiry"]),
            child_position_id=integer(args["childPositionId"]),
            child_execution_state_hash=args["childPositionHash"],
            settlement_kind=None,
            child_shares=shares,
            last_event_block=event.block_number,
        )

    def _settlement_advanced(self, event: FundEvent) -> None:
        args = event.args
        tranche = self._tranche(args["trancheId"])
        child_vault, lane = self._lane(args["lane"])
        leg = _enum(TRANCHE_LEGS, args["leg"], "tranche leg")
        settlement = _enum(
            SETTLEMENT_KINDS, args["settlementKind"], "settlement kind"
        )
        if child_vault != tranche["child_vault"]:
            raise ValueError("Meta Wheel settlement child lane changed")
        valid = (
            tranche["state"] in {"csp_open", "csp_settling"}
            and leg == "csp_settling"
            and lane["lane_type"] == "csp"
            and settlement in {"pending_delivery", "csp_otm", "csp_assigned"}
        ) or (
            tranche["state"] in {"call_open", "call_settling"}
            and leg == "call_settling"
            and lane["lane_type"] == "covered_call"
            and settlement
            in {"pending_delivery", "call_otm", "call_away", "weth_fallback"}
        )
        if not valid:
            raise ValueError("Invalid Meta Wheel settlement transition")
        tranche.update(
            state=leg,
            state_nonce=integer(tranche["state_nonce"]) + 1,
            state_hash=None,
            child_execution_state_hash=args["childPositionHash"],
            settlement_kind=settlement,
            last_event_block=event.block_number,
        )

    def _child_handoff(self, event: FundEvent) -> None:
        args = event.args
        handoff_id = _hex(args["transitionHash"])
        if handoff_id in self.handoffs:
            raise ValueError("Meta Wheel child handoff replayed")
        tranche_id = integer(args["trancheId"])
        tranche = self._tranche(tranche_id)
        child_vault, lane = self._lane(args["lane"])
        if child_vault != tranche["child_vault"]:
            raise ValueError("Meta Wheel handoff child lane changed")
        if integer(args["childSharesBurned"]) != integer(tranche["child_shares"]):
            raise ValueError("Meta Wheel handoff did not burn exact child shares")
        settlement = _enum(
            SETTLEMENT_KINDS, args["settlementKind"], "settlement kind"
        )
        usdc = integer(args["usdcAmount"])
        weth = integer(args["wethAmount"])
        if lane["lane_type"] == "csp":
            if tranche["state"] != "csp_settling" or settlement not in {
                "csp_otm",
                "csp_assigned",
            }:
                raise ValueError("Invalid Meta Wheel CSP handoff")
            direction = "csp_to_call" if weth else "csp_to_csp"
        else:
            if tranche["state"] != "call_settling" or settlement not in {
                "call_otm",
                "call_away",
                "weth_fallback",
            }:
                raise ValueError("Invalid Meta Wheel covered-call handoff")
            direction = "call_to_call" if weth else "call_to_csp"
        if settlement in {"csp_assigned", "call_otm", "weth_fallback"} and weth <= 0:
            raise ValueError("Meta Wheel settlement expected WETH")
        if settlement in {"csp_otm", "call_away"} and weth != 0:
            raise ValueError("Meta Wheel cash settlement unexpectedly returned WETH")

        self.state["pending_csp_usdc"] += usdc
        self.state["transition_weth"] += weth
        tranche.update(
            child_vault=None,
            state="weth_transition" if weth else "pending_csp",
            state_nonce=integer(tranche["state_nonce"]) + 1,
            state_hash=None,
            pending_assets=(
                0 if weth else integer(tranche["pending_assets"]) + usdc
            ),
            child_shares=0,
            child_execution_state_hash=None,
            last_event_block=event.block_number,
        )
        self.handoffs[handoff_id] = {
            "tranche_id": tranche_id,
            "child_vault": child_vault,
            "direction": direction,
            "settlement_kind": settlement,
            "transition_nonce": tranche["state_nonce"],
            "usdc_amount": usdc,
            "weth_amount": weth,
            "child_shares_burned": integer(args["childSharesBurned"]),
            "block_number": event.block_number,
            "transaction_hash": event.transaction_hash,
            "log_index": event.log_index,
        }

    def _assignment_lot_created(self, event: FundEvent) -> None:
        args = event.args
        lot_id = integer(args["lotId"])
        if lot_id in self.lots:
            raise ValueError("Meta Wheel assignment lot created twice")
        tranche_id = integer(args["trancheId"])
        tranche = self._tranche(tranche_id)
        origin, lane = self._lane(args["originCspLane"])
        if tranche["state"] != "csp_settling" or lane["lane_type"] != "csp":
            raise ValueError("Meta Wheel assignment lot has invalid CSP origin")
        received = integer(args["wethReceived"])
        strike = integer(args["literalAssignmentStrike8"])
        if received <= 0 or strike <= 0:
            raise ValueError("Meta Wheel assignment lot must have WETH and strike")
        tranche["assignment_lot_ids"] = [lot_id]
        self.lots[lot_id] = {
            "origin_tranche_id": tranche_id,
            "origin_csp_child_vault": origin,
            "origin_csp_position_id": integer(args["originCspPositionId"]),
            "weth_received": received,
            "remaining_weth": received,
            "literal_assignment_strike_8": strike,
            "status": "available",
            "created_block": event.block_number,
            "last_event_block": event.block_number,
        }

    def _covered_call_floor_enforced(self, event: FundEvent) -> None:
        args = event.args
        tranche = self._tranche(args["trancheId"])
        lot_id = integer(args["lotId"])
        lot = self._lot(lot_id)
        child_vault, lane = self._lane(args["lane"])
        if (
            tranche["state"] != "weth_transition"
            or lane["lane_type"] != "covered_call"
            or self._single_lot_id(tranche) != lot_id
        ):
            raise ValueError("Invalid Meta Wheel covered-call floor binding")
        literal = integer(args["literalAssignmentStrike8"])
        buffer = integer(args["executionCostBuffer8"])
        required = integer(args["requiredFloor8"])
        call_strike = integer(args["callStrike8"])
        if literal != integer(lot["literal_assignment_strike_8"]):
            raise ValueError("Meta Wheel call floor changed assignment basis")
        if required != literal + buffer or call_strike < required:
            raise ValueError("Meta Wheel covered-call strike is below protected floor")
        if self._floor_buffer_observed and buffer != integer(
            self.state["execution_cost_buffer_8"]
        ):
            raise ValueError("Meta Wheel child floor buffer disagrees with coordinator")
        # The registration-time value is not emitted.  The first canonical
        # floor event safely initializes the projection to the enforced value.
        self.state["execution_cost_buffer_8"] = buffer
        self._floor_buffer_observed = True
        tranche.update(
            child_vault=child_vault,
            literal_call_floor_8=literal,
            required_call_floor_8=required,
            call_strike_8=call_strike,
            last_event_block=event.block_number,
        )

    def _lot_status_changed(self, event: FundEvent) -> None:
        args = event.args
        lot = self._lot(args["lotId"])
        tranche_id = integer(args["trancheId"])
        if integer(lot["origin_tranche_id"]) != tranche_id:
            raise ValueError("Meta Wheel lot status changed for another tranche")
        remaining = integer(args["remainingWeth"])
        if remaining > integer(lot["weth_received"]):
            raise ValueError("Meta Wheel lot remaining WETH exceeds assignment")
        status = _enum(LOT_STATUSES, args["status"], "lot status")
        if status == "called_away" and remaining != 0:
            raise ValueError("Called-away Meta Wheel lot retains WETH")
        lot.update(
            status=status,
            remaining_weth=remaining,
            last_event_block=event.block_number,
        )

    def _redemption_reserve_changed(self, event: FundEvent) -> None:
        self.state.update(
            redemption_reserved_usdc=integer(
                event.args["reservedRedemptionUsdc"]
            ),
            pending_csp_usdc=integer(event.args["pendingCspUsdc"]),
        )

    def _redemption_usdc_reserved(self, event: FundEvent) -> None:
        args = event.args
        tranche = self._tranche(args["trancheId"])
        if tranche["state"] != "pending_csp":
            raise ValueError("Meta Wheel redemption reserved from a non-pending tranche")
        amount = integer(args["amount"])
        principal = integer(args["principalReserved"])
        remaining = integer(args["remainingTrancheUsdc"])
        remaining_principal = integer(args["remainingTranchePrincipal"])
        if (
            amount <= 0
            or principal < 0
            or remaining != integer(tranche["pending_assets"]) - amount
            or remaining_principal
            != integer(tranche["principal_assets"]) - principal
            or remaining < 0
            or remaining_principal < 0
        ):
            raise ValueError("Meta Wheel redemption reserve does not reconcile")
        if amount > integer(self.state["pending_csp_usdc"]):
            raise ValueError("Meta Wheel redemption reserve underflows pending CSP")
        tranche.update(
            state="closed" if remaining == 0 else "pending_csp",
            pending_assets=remaining,
            principal_assets=remaining_principal,
            state_nonce=integer(tranche["state_nonce"]) + 1,
            state_hash=None,
            last_event_block=event.block_number,
        )
        self.state["pending_csp_usdc"] -= amount
        self.state["redemption_reserved_usdc"] += amount
        self.state["reserved_principal_usdc"] += principal

    def _redemption_usdc_released(self, event: FundEvent) -> None:
        args = event.args
        tranche_id = integer(args["trancheId"])
        amount = integer(args["amount"])
        principal = integer(args["principalRestored"])
        if (
            tranche_id in self.tranches
            or amount <= 0
            or principal < 0
            or amount > integer(self.state["redemption_reserved_usdc"])
            or principal > integer(self.state["reserved_principal_usdc"])
        ):
            raise ValueError("Invalid Meta Wheel redemption reserve release")
        self.state["redemption_reserved_usdc"] -= amount
        self.state["reserved_principal_usdc"] -= principal
        self.state["pending_csp_usdc"] += amount
        self.tranches[tranche_id] = {
            "allocation_id": None,
            "parent_tranche_id": None,
            "child_vault": None,
            "state": "pending_csp",
            "principal_assets": principal,
            "pending_assets": amount,
            "state_nonce": 1,
            "state_hash": args["stateHash"],
            "expiry": None,
            "child_position_id": None,
            "child_execution_state_hash": None,
            "settlement_kind": None,
            "child_shares": 0,
            "assignment_lot_ids": [],
            "literal_call_floor_8": 0,
            "required_call_floor_8": 0,
            "call_strike_8": None,
            "last_event_block": event.block_number,
        }

    def _accounting_assets_returned(self, event: FundEvent) -> None:
        args = event.args
        amount = integer(args["usdcAmount"])
        reserved = integer(args["reservedConsumed"])
        pending = integer(args["pendingConsumed"])
        if amount <= 0 or amount != reserved + pending:
            raise ValueError("Meta Wheel returned-USDC components do not reconcile")
        if reserved > integer(self.state["redemption_reserved_usdc"]) or pending > integer(
            self.state["pending_csp_usdc"]
        ):
            raise ValueError("Meta Wheel returned-USDC accounting underflow")
        principal_released = _principal_share(
            integer(self.state["reserved_principal_usdc"]),
            integer(self.state["redemption_reserved_usdc"]),
            reserved,
        )
        self.state["redemption_reserved_usdc"] -= reserved
        self.state["reserved_principal_usdc"] -= principal_released
        self.state["pending_csp_usdc"] -= pending

    def _allocation_pause_set(self, event: FundEvent) -> None:
        self.state["paused"] = bool(event.args["paused"])

    def _policy_hash_set(self, event: FundEvent) -> None:
        previous = _hex(event.args["previousPolicyHash"])
        current = self.state["policy_hash"]
        if current is not None and previous != _hex(current):
            raise ValueError("Meta Wheel policy-hash continuity failure")
        self.state.update(
            policy_version=integer(self.state["policy_version"]) + 1,
            policy_hash=_hex(event.args["newPolicyHash"]),
        )

    def _floor_buffer_set(self, event: FundEvent) -> None:
        previous = integer(event.args["previousFloorBufferUsd8"])
        if self._floor_buffer_observed and previous != integer(
            self.state["execution_cost_buffer_8"]
        ):
            raise ValueError("Meta Wheel floor-buffer continuity failure")
        self.state["execution_cost_buffer_8"] = integer(
            event.args["newFloorBufferUsd8"]
        )
        self._floor_buffer_observed = True

    def _premium_accrued(self, event: FundEvent) -> None:
        tranche = self._tranche(event.args["trancheId"])
        child_vault, lane = self._lane(event.args["lane"])
        position_id = integer(event.args["childPositionId"])
        if normalize_address(event.contract_address) != child_vault:
            raise ValueError("Meta Wheel premium does not match the active child position")
        if tranche["child_vault"] is None:
            expected_state = (
                "pending_csp"
                if lane["lane_type"] == "csp"
                else "weth_transition"
            )
            if tranche["state"] != expected_state or position_id <= 0:
                raise ValueError(
                    "Meta Wheel premium does not match an opening child position"
                )
            if integer(event.args["trancheId"]) in self._pending_premium_positions:
                raise ValueError("Meta Wheel opening premium emitted twice")
            self._pending_premium_positions[integer(event.args["trancheId"])] = (
                child_vault,
                position_id,
            )
        elif (
            child_vault != tranche["child_vault"]
            or position_id != integer(tranche["child_position_id"])
        ):
            raise ValueError("Meta Wheel premium does not match the active child position")
        gross = integer(event.args["grossPremiumAssets"])
        fee = integer(event.args["protocolFeeAssets"])
        net = integer(event.args["netPremiumAssets"])
        if gross != fee + net:
            raise ValueError("Meta Wheel premium gross/net/fee mismatch")
        self.state["cumulative_gross_premium"] += gross
        self.state["cumulative_protocol_fee"] += fee
        self.state["cumulative_net_premium"] += net

    def _lane(self, address: Any) -> tuple[str, dict[str, Any]]:
        child_vault = normalize_address(address)
        value = self.lanes.get(child_vault)
        if value is None:
            raise ValueError("Unknown Meta Wheel child lane")
        return child_vault, value

    def _tranche(self, tranche_id: Any) -> dict[str, Any]:
        value = self.tranches.get(integer(tranche_id))
        if value is None:
            raise ValueError("Unknown Meta Wheel tranche")
        return value

    def _lot(self, lot_id: Any) -> dict[str, Any]:
        value = self.lots.get(integer(lot_id))
        if value is None:
            raise ValueError("Unknown Meta Wheel assignment lot")
        return value

    @staticmethod
    def _single_lot_id(tranche: dict[str, Any]) -> int:
        lot_ids = tranche["assignment_lot_ids"]
        if len(lot_ids) != 1:
            raise ValueError("Meta Wheel tranche must bind exactly one assignment lot")
        return integer(lot_ids[0])


def _enum(values: dict[int, str], value: Any, label: str) -> str:
    try:
        return values[integer(value)]
    except KeyError as exc:
        raise ValueError(f"Unknown Meta Wheel {label}") from exc


def _hex(value: Any) -> str:
    return value.hex().lower() if hasattr(value, "hex") else str(value).lower()


def _principal_share(principal: int, total_assets: int, assets: int) -> int:
    if assets == 0:
        return 0
    if total_assets <= 0 or assets > total_assets:
        raise ValueError("Meta Wheel reserved principal cannot be apportioned")
    if assets == total_assets:
        return principal
    return principal * assets // total_assets


def _db_value(value: Any) -> Any:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _db_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _db_value(value) for key, value in row.items()}
