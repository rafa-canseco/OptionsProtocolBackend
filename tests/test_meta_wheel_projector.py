from dataclasses import replace

import pytest

from src.fund_indexer.models import FundEvent
from src.fund_indexer.projector import project_events
from src.meta_wheel.projector import MetaWheelProjection


FUND = "0x00000000000000000000000000000000000000f1"
CSP = "0x0000000000000000000000000000000000000010"
CALL = "0x0000000000000000000000000000000000000020"


def event(index: int, name: str, args: dict, role: str = "wheel_coordinator"):
    if name == "WheelPremiumAccrued":
        role = "wheel_child_lane"
    return FundEvent(
        chain_id=84532,
        fund_address=FUND,
        contract_address=args["lane"] if name == "WheelPremiumAccrued" else FUND,
        contract_role=role,
        interface_version=1,
        block_number=100 + index,
        block_hash=f"0x{100 + index:064x}",
        transaction_hash=f"0x{index + 1:064x}",
        transaction_index=0,
        log_index=index,
        event_name=name,
        args=args,
    )


def events_through_assignment() -> list[FundEvent]:
    return [
        event(0, "WheelLaneRegistered", {"lane": CSP, "kind": 1}),
        event(1, "WheelLaneRegistered", {"lane": CALL, "kind": 2}),
        event(
            2,
            "WheelTrancheQueued",
            {
                "trancheId": 1,
                "allocationId": "0xallocation",
                "usdcAmount": 1_000_000,
                "pendingCspUsdc": 1_000_000,
                "stateHash": "0xqueue",
            },
        ),
        event(
            3,
            "WheelTrancheOpened",
            {
                "trancheId": 1,
                "lane": CSP,
                "leg": 2,
                "childPositionId": 7,
                "childShares": 1_000_000,
                "expiry": 500,
                "childPositionHash": "0xcsp",
            },
        ),
        event(
            4,
            "WheelPremiumAccrued",
            {
                "trancheId": 1,
                "lane": CSP,
                "childPositionId": 7,
                "grossPremiumAssets": 100,
                "protocolFeeAssets": 10,
                "netPremiumAssets": 90,
            },
        ),
        event(
            5,
            "WheelTrancheSettlementAdvanced",
            {
                "trancheId": 1,
                "lane": CSP,
                "leg": 3,
                "settlementKind": 2,
                "childPositionHash": "0xcsp-settled",
            },
        ),
        event(
            6,
            "WheelSiblingTrancheQueued",
            {
                "parentTrancheId": 1,
                "siblingTrancheId": 2,
                "usdcAmount": 90,
                "stateHash": "0xsibling",
            },
        ),
        event(
            7,
            "WheelAssignmentLotCreated",
            {
                "lotId": 9,
                "trancheId": 1,
                "originCspLane": CSP,
                "originCspPositionId": 7,
                "wethReceived": 10**18,
                "literalAssignmentStrike8": 2_000 * 10**8,
            },
        ),
        event(
            8,
            "WheelLotStatusChanged",
            {"lotId": 9, "status": 1, "remainingWeth": 10**18, "trancheId": 1},
        ),
        event(
            9,
            "WheelChildHandoff",
            {
                "trancheId": 1,
                "lane": CSP,
                "transitionHash": "0xhandoff-csp",
                "settlementKind": 2,
                "childSharesBurned": 1_000_000,
                "usdcAmount": 90,
                "wethAmount": 10**18,
            },
        ),
    ]


def full_events() -> list[FundEvent]:
    result = events_through_assignment()
    result.extend(
        [
            event(
                10,
                "WheelCoveredCallFloorEnforced",
                {
                    "trancheId": 1,
                    "lotId": 9,
                    "lane": CALL,
                    "literalAssignmentStrike8": 2_000 * 10**8,
                    "executionCostBuffer8": 10 * 10**8,
                    "requiredFloor8": 2_010 * 10**8,
                    "callStrike8": 2_050 * 10**8,
                },
            ),
            event(
                11,
                "WheelLotStatusChanged",
                {"lotId": 9, "status": 2, "remainingWeth": 10**18, "trancheId": 1},
            ),
            event(
                12,
                "WheelTrancheOpened",
                {
                    "trancheId": 1,
                    "lane": CALL,
                    "leg": 5,
                    "childPositionId": 8,
                    "childShares": 10**18,
                    "expiry": 700,
                    "childPositionHash": "0xcall",
                },
            ),
            event(
                13,
                "WheelPremiumAccrued",
                {
                    "trancheId": 1,
                    "lane": CALL,
                    "childPositionId": 8,
                    "grossPremiumAssets": 200,
                    "protocolFeeAssets": 20,
                    "netPremiumAssets": 180,
                },
            ),
            event(
                14,
                "WheelTrancheSettlementAdvanced",
                {
                    "trancheId": 1,
                    "lane": CALL,
                    "leg": 6,
                    "settlementKind": 4,
                    "childPositionHash": "0xcall-settled",
                },
            ),
            event(
                15,
                "WheelLotStatusChanged",
                {"lotId": 9, "status": 3, "remainingWeth": 0, "trancheId": 1},
            ),
            event(
                16,
                "WheelChildHandoff",
                {
                    "trancheId": 1,
                    "lane": CALL,
                    "transitionHash": "0xhandoff-call",
                    "settlementKind": 4,
                    "childSharesBurned": 10**18,
                    "usdcAmount": 2_100_000,
                    "wethAmount": 0,
                },
            ),
        ]
    )
    return result


def test_wheel_projection_tracks_exact_contract_state_machine() -> None:
    projection = MetaWheelProjection(84532, FUND)
    for item in full_events():
        assert projection.apply(item)
    exported = projection.export()
    state = exported["wheel_state"][0]
    tranche = exported["wheel_tranches"][0]
    lot = exported["wheel_assignment_lots"][0]

    assert exported["wheel_lanes"][0]["child_vault"] == CSP
    assert tranche["tranche_id"] == 1
    assert tranche["child_vault"] is None
    assert tranche["state"] == "pending_csp"
    assert tranche["assignment_lot_ids"] == [9]
    assert tranche["required_call_floor_8"] == str(2_010 * 10**8)
    assert lot["origin_csp_child_vault"] == CSP
    assert lot["status"] == "called_away"
    assert state["cumulative_gross_premium"] == "300"
    assert state["cumulative_protocol_fee"] == "30"
    assert state["cumulative_net_premium"] == "270"
    assert state["pending_csp_usdc"] == "2100090"
    assert exported["wheel_tranches"][1]["parent_tranche_id"] == "1"
    assert exported["wheel_tranches"][1]["pending_assets"] == "90"
    assert len(exported["wheel_handoffs"]) == 2


def test_wheel_projection_rejects_call_below_assignment_floor() -> None:
    projection = MetaWheelProjection(84532, FUND)
    for item in events_through_assignment():
        projection.apply(item)
    below = full_events()[10]
    below.args["callStrike8"] = 2_009 * 10**8
    with pytest.raises(ValueError, match="below protected floor"):
        projection.apply(below)


def test_wheel_projection_rejects_handoff_replay() -> None:
    projection = MetaWheelProjection(84532, FUND)
    items = events_through_assignment()
    for item in items:
        projection.apply(item)
    replay = replace(items[-1], log_index=items[-1].log_index + 100)
    with pytest.raises(ValueError, match="replayed"):
        projection.apply(replay)


def test_wheel_projection_is_never_activated_for_standalone_funds() -> None:
    projection = project_events(
        [event(0, "WheelAllocationPauseSet", {"paused": True})],
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
        strategy_kind="csp",
    )
    assert projection.wheel is None
    assert "wheel_state" not in projection.export()
