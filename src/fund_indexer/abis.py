from typing import Any

from web3 import Web3


def _event(
    name: str,
    inputs: list[tuple[str, str, bool]],
) -> dict[str, Any]:
    return {
        "anonymous": False,
        "type": "event",
        "name": name,
        "inputs": [
            {"name": field, "type": solidity_type, "indexed": indexed}
            for field, solidity_type, indexed in inputs
        ],
    }


EVENT_ABIS = [
    _event(
        "Transfer",
        [
            ("from", "address", True),
            ("to", "address", True),
            ("value", "uint256", False),
        ],
    ),
    _event(
        "Deposit",
        [
            ("sender", "address", True),
            ("owner", "address", True),
            ("assets", "uint256", False),
            ("shares", "uint256", False),
        ],
    ),
    _event(
        "Withdraw",
        [
            ("sender", "address", True),
            ("receiver", "address", True),
            ("owner", "address", True),
            ("assets", "uint256", False),
            ("shares", "uint256", False),
        ],
    ),
    _event(
        "RedeemRequest",
        [
            ("controller", "address", True),
            ("owner", "address", True),
            ("requestId", "uint256", True),
            ("sender", "address", False),
            ("shares", "uint256", False),
        ],
    ),
    _event(
        "ClaimReserved",
        [
            ("controller", "address", True),
            ("shares", "uint256", False),
            ("assets", "uint256", False),
        ],
    ),
    _event(
        "NavCommitted",
        [
            ("reportNonce", "uint64", True),
            ("netAssets", "uint256", False),
            ("validAfterBlock", "uint64", False),
            ("validUntilBlock", "uint64", False),
        ],
    ),
    _event("NavInvalidated", [("positionsHash", "bytes32", True)]),
    _event(
        "NavWindowRestored",
        [("reportNonce", "uint64", True), ("validUntilBlock", "uint64", False)],
    ),
    _event(
        "NavSubmitted",
        [
            ("reportNonce", "uint64", True),
            ("reportHash", "bytes32", True),
            ("netAssets", "uint256", False),
            ("feeShares", "uint256", False),
        ],
    ),
    _event(
        "ComponentUpdated",
        [
            ("componentId", "bytes32", True),
            ("valuator", "address", False),
            ("interfaceVersion", "uint64", False),
            ("active", "bool", False),
        ],
    ),
    _event(
        "ComponentStateUpdated",
        [
            ("componentId", "bytes32", True),
            ("nonce", "uint64", False),
            ("positionStateHash", "bytes32", False),
        ],
    ),
    _event(
        "FeeConfigUpdated",
        [
            ("recipient", "address", True),
            ("managementFeeWad", "uint64", False),
            ("performanceFeeBps", "uint16", False),
        ],
    ),
    _event(
        "ReporterSetUpdated",
        [
            ("version", "uint64", True),
            ("threshold", "uint16", False),
            ("reporters", "address[]", False),
        ],
    ),
    _event(
        "RedeemBatchSealed",
        [("batchId", "uint64", True), ("pendingShares", "uint256", False)],
    ),
    _event(
        "RedeemBatchStarted",
        [
            ("batchId", "uint64", True),
            ("shares", "uint256", False),
            ("processingNav", "uint256", False),
            ("reservedAssets", "uint256", False),
            ("marginalExitCost", "uint256", False),
        ],
    ),
    _event(
        "RedeemBatchProcessed",
        [
            ("batchId", "uint64", True),
            ("controllers", "uint16", False),
            ("roundComplete", "bool", False),
        ],
    ),
    _event(
        "ClaimConsumed",
        [
            ("controller", "address", True),
            ("shares", "uint256", False),
            ("assets", "uint256", False),
        ],
    ),
    _event(
        "PendingCancelled",
        [("controller", "address", True), ("shares", "uint256", False)],
    ),
    _event("RedeemBatchReleased", [("batchId", "uint64", True)]),
    _event(
        "StrategyConfigured",
        [
            ("adapter", "address", True),
            ("valuator", "address", True),
            ("active", "bool", False),
        ],
    ),
    _event(
        "StrategyAllocated",
        [
            ("adapter", "address", True),
            ("asset", "address", True),
            ("amount", "uint256", False),
            ("positionNonce", "uint64", False),
        ],
    ),
    _event(
        "StrategyDeallocated",
        [
            ("adapter", "address", True),
            ("targetValue", "uint256", False),
            ("assetsOut", "uint256", False),
            ("positionNonce", "uint64", False),
        ],
    ),
    _event(
        "StrategyDeallocatedInKind",
        [
            ("batchId", "bytes32", True),
            ("adapter", "address", True),
            ("escrow", "address", True),
            ("fractionWad", "uint256", False),
            ("positionNonce", "uint64", False),
        ],
    ),
    _event(
        "StrategyEmergencyExited",
        [
            ("adapter", "address", True),
            ("escrow", "address", True),
            ("positionNonce", "uint64", False),
        ],
    ),
    _event(
        "PositionOpened",
        [
            ("positionId", "uint256", True),
            ("protocolVaultId", "uint256", True),
            ("oToken", "address", True),
            ("marketMaker", "address", False),
            ("optionAmount", "uint256", False),
            ("collateral", "uint256", False),
            ("premiumEarned", "uint256", False),
            ("lifecycleHash", "bytes32", False),
        ],
    ),
    _event(
        "PositionTransitioned",
        [
            ("positionId", "uint256", True),
            ("protocolVaultId", "uint256", True),
            ("lifecycle", "uint8", False),
            ("collateralDelta", "uint256", False),
            ("payment", "uint256", False),
            ("wethDelta", "uint256", False),
            ("lifecycleHash", "bytes32", False),
        ],
    ),
    _event("AccountingAssetsReturned", [("amount", "uint256", False)]),
    _event(
        "AssignedWethSwapped",
        [("wethIn", "uint256", False), ("usdcOut", "uint256", False)],
    ),
    _event(
        "UsdcNormalized",
        [("usdcIn", "uint256", False), ("wethOut", "uint256", False)],
    ),
    _event(
        "RawAssetsRecovered",
        [
            ("escrow", "address", True),
            ("assets", "address[]", False),
            ("amounts", "uint256[]", False),
            ("emergency", "bool", False),
        ],
    ),
    _event(
        "UnaccountedAssetIsolated",
        [("asset", "address", True), ("amount", "uint256", False)],
    ),
    _event(
        "OrderExecuted",
        [
            ("user", "address", True),
            ("oToken", "address", True),
            ("mm", "address", True),
            ("amount", "uint256", False),
            ("grossPremium", "uint256", False),
            ("netPremium", "uint256", False),
            ("fee", "uint256", False),
            ("collateral", "uint256", False),
            ("vaultId", "uint256", False),
        ],
    ),
    _event(
        "PhysicalDeliveryReserved",
        [
            ("owner", "address", True),
            ("vaultId", "uint256", True),
            ("mm", "address", True),
            ("oToken", "address", False),
            ("amount", "uint256", False),
        ],
    ),
    _event(
        "PhysicalDeliveryReleased",
        [
            ("owner", "address", True),
            ("vaultId", "uint256", True),
            ("mm", "address", True),
            ("oToken", "address", False),
            ("amount", "uint256", False),
        ],
    ),
    _event(
        "PhysicalDelivery",
        [
            ("oToken", "address", True),
            ("user", "address", True),
            ("contraAmount", "uint256", False),
            ("collateralUsed", "uint256", False),
        ],
    ),
    _event(
        "PhysicalDeliverySettled",
        [
            ("owner", "address", True),
            ("vaultId", "uint256", True),
            ("mm", "address", True),
            ("oToken", "address", False),
            ("amount", "uint256", False),
            ("payoutReceiver", "address", False),
            ("payout", "uint256", False),
        ],
    ),
    _event("VaultOpened", [("owner", "address", True), ("vaultId", "uint256", False)]),
    _event(
        "VaultSettled",
        [
            ("owner", "address", True),
            ("vaultId", "uint256", False),
            ("collateralReturned", "uint256", False),
        ],
    ),
    _event(
        "Redeemed",
        [
            ("oToken", "address", True),
            ("redeemer", "address", True),
            ("otokenAmount", "uint256", False),
            ("payout", "uint256", False),
        ],
    ),
    _event("Upgraded", [("implementation", "address", True)]),
]


def event_topic(abi: dict[str, Any]) -> str:
    types = ",".join(item["type"] for item in abi["inputs"])
    return Web3.to_hex(Web3.keccak(text=f"{abi['name']}({types})"))


EVENTS_BY_TOPIC = {event_topic(abi): abi for abi in EVENT_ABIS}
