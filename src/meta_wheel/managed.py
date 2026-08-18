"""Frozen Meta Wheel managed-operation ABI helpers.

Every coordinator mutation other than the normal StrategyManager ``allocate`` /
``deallocate`` path is wrapped by one role-separated StrategyManager selector.
Keeping the discriminants and nested encoding here prevents backend tooling from
accidentally preparing a direct coordinator call or using the wrong role class.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from eth_abi import encode
from web3 import Web3


class ManagedOperationClass(IntEnum):
    NONE = 0
    ALLOCATION = 1
    PROCESSING = 2
    GUARDIAN = 3
    CONFIGURATION = 4


class ManagedOperation(IntEnum):
    NONE = 0
    OPEN_CSP = 1
    OPEN_COVERED_CALL = 2
    SPLIT_PENDING_CSP = 3
    SETTLE_CSP = 4
    HANDOFF_CSP = 5
    SETTLE_COVERED_CALL = 6
    HANDOFF_COVERED_CALL = 7
    RESERVE_REDEMPTION = 8
    RELEASE_REDEMPTION = 9
    PAUSE_ALLOCATIONS = 10
    REGISTER_LANE = 11
    REMOVE_LANE = 12
    SET_LANE_ACTIVE = 13
    SET_POLICY_HASH = 14
    SET_FLOOR_BUFFER = 15
    RESUME_ALLOCATIONS = 16


class StrategyManagerWrapper(StrEnum):
    ALLOCATION = "executeAdapterAllocationOperation"
    PROCESSING = "executeAdapterProcessingOperation"
    GUARDIAN = "executeAdapterGuardianOperation"
    CONFIGURATION = "executeAdapterConfigurationOperation"

    @property
    def signature(self) -> str:
        return f"{self.value}(address,bytes)"


@dataclass(frozen=True, slots=True)
class ManagedOperationRequest:
    """Exact dispatcher payload and outer StrategyManager calldata."""

    adapter: str
    wrapper: StrategyManagerWrapper
    operation_class: ManagedOperationClass
    operation: ManagedOperation
    arguments: bytes
    data: bytes
    calldata: bytes


_MANAGED_OPERATION_SPECS: dict[
    ManagedOperation, tuple[ManagedOperationClass, tuple[str, ...]]
] = {
    ManagedOperation.OPEN_CSP: (
        ManagedOperationClass.ALLOCATION,
        ("uint256", "address", "bytes"),
    ),
    ManagedOperation.OPEN_COVERED_CALL: (
        ManagedOperationClass.ALLOCATION,
        ("uint256", "address", "bytes"),
    ),
    ManagedOperation.SPLIT_PENDING_CSP: (
        ManagedOperationClass.ALLOCATION,
        ("uint256", "uint256"),
    ),
    ManagedOperation.SETTLE_CSP: (ManagedOperationClass.PROCESSING, ("uint256",)),
    ManagedOperation.HANDOFF_CSP: (ManagedOperationClass.PROCESSING, ("uint256",)),
    ManagedOperation.SETTLE_COVERED_CALL: (
        ManagedOperationClass.PROCESSING,
        ("uint256",),
    ),
    ManagedOperation.HANDOFF_COVERED_CALL: (
        ManagedOperationClass.PROCESSING,
        ("uint256",),
    ),
    ManagedOperation.RESERVE_REDEMPTION: (
        ManagedOperationClass.PROCESSING,
        ("uint256", "uint256"),
    ),
    ManagedOperation.RELEASE_REDEMPTION: (
        ManagedOperationClass.PROCESSING,
        ("uint256",),
    ),
    ManagedOperation.PAUSE_ALLOCATIONS: (ManagedOperationClass.GUARDIAN, ()),
    ManagedOperation.REGISTER_LANE: (
        ManagedOperationClass.CONFIGURATION,
        ("address", "uint8"),
    ),
    ManagedOperation.REMOVE_LANE: (
        ManagedOperationClass.CONFIGURATION,
        ("address",),
    ),
    ManagedOperation.SET_LANE_ACTIVE: (
        ManagedOperationClass.CONFIGURATION,
        ("address", "bool"),
    ),
    ManagedOperation.SET_POLICY_HASH: (
        ManagedOperationClass.CONFIGURATION,
        ("bytes32",),
    ),
    ManagedOperation.SET_FLOOR_BUFFER: (
        ManagedOperationClass.CONFIGURATION,
        ("uint256",),
    ),
    ManagedOperation.RESUME_ALLOCATIONS: (
        ManagedOperationClass.CONFIGURATION,
        (),
    ),
}

_WRAPPER_BY_CLASS = {
    ManagedOperationClass.ALLOCATION: StrategyManagerWrapper.ALLOCATION,
    ManagedOperationClass.PROCESSING: StrategyManagerWrapper.PROCESSING,
    ManagedOperationClass.GUARDIAN: StrategyManagerWrapper.GUARDIAN,
    ManagedOperationClass.CONFIGURATION: StrategyManagerWrapper.CONFIGURATION,
}


def encode_managed_operation(
    adapter: str, operation: ManagedOperation, *values: object
) -> ManagedOperationRequest:
    """Encode ``abi.encode(operation, arguments)`` and its manager wrapper."""

    if not Web3.is_address(adapter) or int(adapter, 16) == 0:
        raise ValueError("Meta Wheel adapter must be a non-zero address")
    try:
        operation_class, argument_types = _MANAGED_OPERATION_SPECS[operation]
    except KeyError as error:
        raise ValueError(
            f"Unsupported managed Wheel operation {operation!r}"
        ) from error
    if len(values) != len(argument_types):
        raise ValueError(
            f"{operation.name} expects {len(argument_types)} arguments, "
            f"received {len(values)}"
        )
    arguments = encode(list(argument_types), list(values)) if argument_types else b""
    data = encode(["uint8", "bytes"], [int(operation), arguments])
    wrapper = _WRAPPER_BY_CLASS[operation_class]
    selector = Web3.keccak(text=wrapper.signature)[:4]
    calldata = selector + encode(["address", "bytes"], [adapter, data])
    return ManagedOperationRequest(
        adapter=adapter.lower(),
        wrapper=wrapper,
        operation_class=operation_class,
        operation=operation,
        arguments=arguments,
        data=data,
        calldata=calldata,
    )
