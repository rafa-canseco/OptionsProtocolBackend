from eth_abi import decode, encode
from web3 import Web3

from src.meta_wheel.managed import (
    ManagedOperation,
    ManagedOperationClass,
    StrategyManagerWrapper,
    encode_managed_operation,
)


COORDINATOR = "0x00000000000000000000000000000000000000c0"
LANE = "0x00000000000000000000000000000000000000a1"


def test_managed_operation_discriminants_and_outer_manager_calldata() -> None:
    request = encode_managed_operation(
        COORDINATOR,
        ManagedOperation.RESERVE_REDEMPTION,
        7,
        1_500 * 10**6,
    )
    expected_arguments = encode(["uint256", "uint256"], [7, 1_500 * 10**6])
    expected_selector = Web3.keccak(
        text="executeAdapterProcessingOperation(address,bytes)"
    )[:4]

    assert request.wrapper == StrategyManagerWrapper.PROCESSING
    assert request.operation_class == ManagedOperationClass.PROCESSING
    assert request.arguments == expected_arguments
    assert request.data == encode(
        ["uint8", "bytes"],
        [int(ManagedOperation.RESERVE_REDEMPTION), expected_arguments],
    )
    assert request.calldata[:4] == expected_selector
    adapter, data = decode(["address", "bytes"], request.calldata[4:])
    assert adapter.lower() == COORDINATOR
    assert data == request.data


def test_remove_and_register_lane_use_configuration_class() -> None:
    remove = encode_managed_operation(COORDINATOR, ManagedOperation.REMOVE_LANE, LANE)
    operation, arguments = decode(["uint8", "bytes"], remove.data)
    (decoded_lane,) = decode(["address"], arguments)

    assert remove.wrapper == StrategyManagerWrapper.CONFIGURATION
    assert remove.operation_class == ManagedOperationClass.CONFIGURATION
    assert operation == ManagedOperation.REMOVE_LANE
    assert decoded_lane.lower() == LANE

    register = encode_managed_operation(
        COORDINATOR, ManagedOperation.REGISTER_LANE, LANE, 1
    )
    operation, arguments = decode(["uint8", "bytes"], register.data)
    decoded_lane, kind = decode(["address", "uint8"], arguments)
    assert operation == ManagedOperation.REGISTER_LANE
    assert (decoded_lane.lower(), kind) == (LANE, 1)


def test_managed_operation_rejects_wrong_arity_and_direct_none() -> None:
    try:
        encode_managed_operation(COORDINATOR, ManagedOperation.OPEN_CSP, 1)
    except ValueError as error:
        assert "expects 3 arguments" in str(error)
    else:
        raise AssertionError("wrong arity must fail")

    try:
        encode_managed_operation(COORDINATOR, ManagedOperation.NONE)
    except ValueError as error:
        assert "Unsupported managed Wheel operation" in str(error)
    else:
        raise AssertionError("NONE must never produce calldata")
