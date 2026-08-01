"""Minimal ABIs copied from the generated B1N-352 handoff."""

from typing import Any


def _function(name: str, inputs: list[dict], outputs: list[dict], state="view"):
    return {
        "type": "function",
        "name": name,
        "inputs": inputs,
        "outputs": outputs,
        "stateMutability": state,
    }


def _field(name: str, solidity_type: str, components=None) -> dict[str, Any]:
    value = {"name": name, "type": solidity_type}
    if components:
        value["components"] = components
    return value


COMPONENT_FIELDS = [
    _field("fund", "address"),
    _field("componentId", "bytes32"),
    _field("chainId", "uint256"),
    _field("snapshotBlock", "uint64"),
    _field("snapshotBlockHash", "bytes32"),
    _field("validAfterBlock", "uint64"),
    _field("validUntilBlock", "uint64"),
    _field("reporterSetVersion", "uint64"),
    _field("componentNonce", "uint64"),
    _field("positionStateHash", "bytes32"),
    _field("grossAssets", "uint256"),
    _field("liabilities", "uint256"),
    _field("liquidAccountingAssets", "uint256"),
    _field("baseExitCost", "uint256"),
    _field("dataHash", "bytes32"),
]
COMPONENT_STATE_FIELDS = [
    _field("valuator", "address"),
    _field("interfaceVersion", "uint64"),
    _field("nonce", "uint64"),
    _field("positionStateHash", "bytes32"),
    _field("active", "bool"),
]
NAV_FIELDS = [
    _field("grossAssets", "uint256"),
    _field("liabilities", "uint256"),
    _field("netAssets", "uint256"),
    _field("liquidAccountingAssets", "uint256"),
    _field("baseExitCost", "uint256"),
    _field("snapshotBlock", "uint64"),
    _field("validAfterBlock", "uint64"),
    _field("validUntilBlock", "uint64"),
    _field("reporterSetVersion", "uint64"),
    _field("reportNonce", "uint64"),
    _field("positionsHash", "bytes32"),
    _field("reportHash", "bytes32"),
    _field("signaturesHash", "bytes32"),
    _field("fundFlowNonce", "uint64"),
    _field("idleStateHash", "bytes32"),
]

ACCOUNTING_ABI = [
    _function("activeComponentCount", [], [_field("", "uint256")]),
    _function(
        "activeComponentAt", [_field("index", "uint256")], [_field("", "bytes32")]
    ),
    _function(
        "componentState",
        [_field("componentId", "bytes32")],
        [_field("", "tuple", COMPONENT_STATE_FIELDS)],
    ),
    _function("activeReporterCount", [], [_field("", "uint256")]),
    _function(
        "activeReporterAt", [_field("index", "uint256")], [_field("", "address")]
    ),
    _function("reporterSetVersion", [], [_field("", "uint64")]),
    _function("reporterThreshold", [], [_field("", "uint16")]),
    _function("lastReportNonce", [], [_field("", "uint64")]),
    _function(
        "navPolicy",
        [],
        [
            _field("activationDelay", "uint64"),
            _field("maxSnapshotAge", "uint64"),
            _field("maxWindowLength", "uint64"),
        ],
    ),
    _function(
        "signatureDigest",
        [
            _field("reportNonce", "uint64"),
            _field("reports", "tuple[]", COMPONENT_FIELDS),
        ],
        [_field("", "bytes32")],
    ),
    _function(
        "submitNav",
        [
            _field("reportNonce", "uint64"),
            _field("reports", "tuple[]", COMPONENT_FIELDS),
            _field("reporters", "address[]"),
            _field("signatures", "bytes[]"),
        ],
        [_field("nav", "tuple", NAV_FIELDS)],
        "nonpayable",
    ),
]

VAULT_ABI = [
    _function("asset", [], [_field("", "address")]),
    _function("fundFlowNonce", [], [_field("", "uint64")]),
    _function("idleStateHash", [], [_field("", "bytes32")]),
]
FLOW_ABI = [_function("hasActiveProcessing", [], [_field("", "bool")])]
STRATEGY_ABI = [_function("positionsHash", [], [_field("", "bytes32")])]
ERC20_ABI = [
    _function("balanceOf", [_field("account", "address")], [_field("", "uint256")]),
    _function("decimals", [], [_field("", "uint8")]),
]
ACCESS_ABI = [
    _function(
        "hasRole",
        [_field("roleId", "uint64"), _field("account", "address")],
        [_field("isMember", "bool"), _field("executionDelay", "uint32")],
    ),
    _function(
        "getTargetFunctionRole",
        [_field("target", "address"), _field("selector", "bytes4")],
        [_field("", "uint64")],
    ),
]

POSITION_FIELDS = [
    _field("oToken", "address"),
    _field("marketMaker", "address"),
    _field("protocolVaultId", "uint256"),
    _field("optionAmount", "uint256"),
    _field("collateral", "uint256"),
    _field("premiumEarned", "uint256"),
    _field("collateralReturned", "uint256"),
    _field("assignedWeth", "uint256"),
    _field("wethBalanceBeforeDelivery", "uint256"),
    _field("openedAt", "uint64"),
    _field("fallbackEligibleAt", "uint64"),
    _field("lifecycle", "uint8"),
    _field("lifecycleHash", "bytes32"),
]
ADAPTER_STATE_FIELDS = [
    _field("stateNonce", "uint64"),
    _field("positionsHash", "bytes32"),
    _field("positionCount", "uint256"),
    _field("activePositionCount", "uint256"),
    _field("accountedUsdc", "uint256"),
    _field("accountedWeth", "uint256"),
]
ADAPTER_ABI = [
    _function("interfaceVersion", [], [_field("", "uint64")]),
    _function("accountingAsset", [], [_field("", "address")]),
    _function("weth", [], [_field("", "address")]),
    _function("adapterState", [], [_field("state", "tuple", ADAPTER_STATE_FIELDS)]),
    _function(
        "position",
        [_field("positionId", "uint256")],
        [_field("", "tuple", POSITION_FIELDS)],
    ),
    _function("positionStateHash", [], [_field("", "bytes32")]),
]

COVERED_CALL_POSITION_FIELDS = [
    _field("oToken", "address"),
    _field("marketMaker", "address"),
    _field("protocolVaultId", "uint256"),
    _field("optionAmount", "uint256"),
    _field("collateral", "uint256"),
    _field("premiumEarned", "uint256"),
    _field("collateralReturned", "uint256"),
    _field("calledAwayUsdc", "uint256"),
    _field("fallbackWethRecovered", "uint256"),
    _field("mmWethPayout", "uint256"),
    _field("usdcBalanceBeforeDelivery", "uint256"),
    _field("openedAt", "uint64"),
    _field("fallbackEligibleAt", "uint64"),
    _field("lifecycle", "uint8"),
    _field("lifecycleHash", "bytes32"),
]
COVERED_CALL_ADAPTER_STATE_FIELDS = [
    _field("stateNonce", "uint64"),
    _field("positionsHash", "bytes32"),
    _field("positionCount", "uint256"),
    _field("activePositionCount", "uint256"),
    _field("activeCollateral", "uint256"),
    _field("accountedWeth", "uint256"),
    _field("accountedUsdc", "uint256"),
]
COVERED_CALL_ADAPTER_ABI = [
    _function("interfaceVersion", [], [_field("", "uint64")]),
    _function("accountingAsset", [], [_field("", "address")]),
    _function("weth", [], [_field("", "address")]),
    _function("usdc", [], [_field("", "address")]),
    _function(
        "adapterState",
        [],
        [_field("state", "tuple", COVERED_CALL_ADAPTER_STATE_FIELDS)],
    ),
    _function(
        "position",
        [_field("positionId", "uint256")],
        [_field("", "tuple", COVERED_CALL_POSITION_FIELDS)],
    ),
    _function("positionStateHash", [], [_field("", "bytes32")]),
]

WHEEL_SUMMARY_FIELDS = [
    _field("stateNonce", "uint64"),
    _field("trancheCount", "uint256"),
    _field("assignmentLotCount", "uint256"),
    _field("pendingCspUsdc", "uint256"),
    _field("reservedRedemptionUsdc", "uint256"),
    _field("transitionWeth", "uint256"),
    _field("accountedUsdc", "uint256"),
    _field("accountedWeth", "uint256"),
]
WHEEL_COORDINATOR_ABI = [
    _function("interfaceVersion", [], [_field("", "uint64")]),
    _function("accountingAsset", [], [_field("", "address")]),
    _function("weth", [], [_field("", "address")]),
    _function("summary", [], [_field("state", "tuple", WHEEL_SUMMARY_FIELDS)]),
    _function("positionStateHash", [], [_field("", "bytes32")]),
]

POSITION_VALUE_FIELDS = [
    _field("grossAssets", "uint256"),
    _field("liabilities", "uint256"),
    _field("liquidAccountingAssets", "uint256"),
    _field("baseExitCost", "uint256"),
    _field("dataHash", "bytes32"),
]
VALUATOR_ABI = [
    _function(
        "value",
        [
            _field("adapter", "address"),
            _field("snapshotBlock", "uint64"),
            _field("data", "bytes"),
        ],
        [_field("positionValue", "tuple", POSITION_VALUE_FIELDS)],
    ),
    _function("observationQuorum", [], [_field("", "uint8")]),
    _function("interfaceVersion", [], [_field("", "uint64")]),
    _function("valuationPolicyVersion", [], [_field("", "uint64")]),
    _function("requiredModelVersion", [], [_field("", "uint64")]),
    _function("maxObservationWindow", [], [_field("", "uint64")]),
    _function("liabilityBufferBps", [], [_field("", "uint16")]),
    _function("maxObservationDivergenceBps", [], [_field("", "uint16")]),
    _function("spotFeed", [], [_field("", "address")]),
    _function("spotFeedDecimals", [], [_field("", "uint8")]),
    _function("maxSpotStaleness", [], [_field("", "uint64")]),
    _function(
        "isApprovedObserver",
        [_field("observer", "address")],
        [_field("approved", "bool")],
    ),
    _function(
        "observationDigest",
        [
            _field("adapter", "address"),
            _field("positionId", "uint256"),
            _field("snapshotBlock", "uint64"),
            _field("validUntilBlock", "uint64"),
            _field("liability", "uint256"),
            _field("baseExitCost", "uint256"),
            _field("nonce", "uint256"),
        ],
        [_field("", "bytes32")],
    ),
]
OTOKEN_ABI = [
    _function("underlying", [], [_field("", "address")]),
    _function("strikeAsset", [], [_field("", "address")]),
    _function("collateralAsset", [], [_field("", "address")]),
    _function("strikePrice", [], [_field("", "uint256")]),
    _function("expiry", [], [_field("", "uint256")]),
    _function("isPut", [], [_field("", "bool")]),
]

CHAINLINK_SPOT_ABI = [
    _function("decimals", [], [_field("", "uint8")]),
    _function(
        "latestRoundData",
        [],
        [
            _field("roundId", "uint80"),
            _field("answer", "int256"),
            _field("startedAt", "uint256"),
            _field("updatedAt", "uint256"),
            _field("answeredInRound", "uint80"),
        ],
    ),
]
