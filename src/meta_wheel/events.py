"""Canonical WheelCoordinator event ABI boundary.

The parent coordinator deliberately re-emits the lifecycle facts required by
the backend, so the fund indexer never needs to discover or poll child-lane
addresses while scanning a block window.
"""

from typing import Any

from web3 import Web3


def _event(name: str, inputs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    return {
        "anonymous": False,
        "type": "event",
        "name": name,
        "inputs": [
            {"name": field, "type": solidity_type, "indexed": indexed}
            for field, solidity_type, indexed in inputs
        ],
    }


# WheelTypes enums are encoded as uint8 in event signatures.
WHEEL_EVENT_ABIS = [
    _event(
        "WheelLaneRegistered",
        [("lane", "address", True), ("kind", "uint8", True)],
    ),
    _event(
        "WheelLaneStatusSet",
        [("lane", "address", True), ("active", "bool", False)],
    ),
    _event(
        "WheelTrancheQueued",
        [
            ("trancheId", "uint256", True),
            ("allocationId", "bytes32", True),
            ("usdcAmount", "uint256", False),
            ("pendingCspUsdc", "uint256", False),
            ("stateHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelSiblingTrancheQueued",
        [
            ("parentTrancheId", "uint256", True),
            ("siblingTrancheId", "uint256", True),
            ("usdcAmount", "uint256", False),
            ("stateHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelTrancheOpened",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("leg", "uint8", False),
            ("childPositionId", "uint256", False),
            ("childShares", "uint256", False),
            ("expiry", "uint64", False),
            ("childPositionHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelTrancheSettlementAdvanced",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("leg", "uint8", False),
            ("settlementKind", "uint8", False),
            ("childPositionHash", "bytes32", False),
        ],
    ),
    _event(
        "WheelChildHandoff",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("transitionHash", "bytes32", True),
            ("settlementKind", "uint8", False),
            ("childSharesBurned", "uint256", False),
            ("usdcAmount", "uint256", False),
            ("wethAmount", "uint256", False),
        ],
    ),
    _event(
        "WheelAssignmentLotCreated",
        [
            ("lotId", "uint256", True),
            ("trancheId", "uint256", True),
            ("originCspLane", "address", True),
            ("originCspPositionId", "uint256", False),
            ("wethReceived", "uint256", False),
            ("literalAssignmentStrike8", "uint256", False),
        ],
    ),
    _event(
        "WheelCoveredCallFloorEnforced",
        [
            ("trancheId", "uint256", True),
            ("lotId", "uint256", True),
            ("lane", "address", True),
            ("literalAssignmentStrike8", "uint256", False),
            ("executionCostBuffer8", "uint256", False),
            ("requiredFloor8", "uint256", False),
            ("callStrike8", "uint256", False),
        ],
    ),
    _event(
        "WheelLotStatusChanged",
        [
            ("lotId", "uint256", True),
            ("status", "uint8", False),
            ("remainingWeth", "uint256", False),
            ("trancheId", "uint256", False),
        ],
    ),
    _event(
        "WheelRedemptionReserveChanged",
        [
            ("reservedRedemptionUsdc", "uint256", False),
            ("pendingCspUsdc", "uint256", False),
        ],
    ),
    _event(
        "WheelAccountingAssetsReturned",
        [
            ("usdcAmount", "uint256", False),
            ("reservedConsumed", "uint256", False),
            ("pendingConsumed", "uint256", False),
        ],
    ),
    _event("WheelAllocationPauseSet", [("paused", "bool", False)]),
    _event(
        "WheelPolicyHashSet",
        [
            ("previousPolicyHash", "bytes32", True),
            ("newPolicyHash", "bytes32", True),
        ],
    ),
    _event(
        "WheelFloorBufferSet",
        [
            ("previousFloorBufferUsd8", "uint256", False),
            ("newFloorBufferUsd8", "uint256", False),
        ],
    ),
    # Canonical fee telemetry is emitted by the coordinator for each child
    # execution.  It is kept parent-scoped so a replay never has to correlate
    # mutable child adapter bindings.
    _event(
        "WheelPremiumAccrued",
        [
            ("trancheId", "uint256", True),
            ("lane", "address", True),
            ("childPositionId", "uint256", True),
            ("grossPremiumAssets", "uint256", False),
            ("protocolFeeAssets", "uint256", False),
            ("netPremiumAssets", "uint256", False),
        ],
    ),
]


def event_topic(abi: dict[str, Any]) -> str:
    types = ",".join(item["type"] for item in abi["inputs"])
    return Web3.to_hex(Web3.keccak(text=f"{abi['name']}({types})"))


WHEEL_EVENTS_BY_TOPIC = {event_topic(abi): abi for abi in WHEEL_EVENT_ABIS}
WHEEL_EVENT_NAMES = frozenset(abi["name"] for abi in WHEEL_EVENT_ABIS)
