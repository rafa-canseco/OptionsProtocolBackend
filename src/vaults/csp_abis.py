"""Minimal read-only ABIs for the Base Sepolia ETH/USDC CSP vault."""


def _view(name: str, inputs: list[dict], outputs: list[dict]) -> dict:
    return {
        "type": "function",
        "name": name,
        "inputs": inputs,
        "outputs": outputs,
        "stateMutability": "view",
    }


def _uint(name: str = "") -> dict:
    return {"name": name, "type": "uint256"}


def _address(name: str = "") -> dict:
    return {"name": name, "type": "address"}


ETH_CSP_VAULT_ABI = [
    _view("totalManagedAssets", [], [_uint()]),
    _view("totalShares", [], [_uint()]),
    _view("availableIdleAssets", [], [_uint()]),
    _view("activeCollateral", [], [_uint()]),
    _view("activeBatches", [], [_uint()]),
    _view("currentEpoch", [], [_uint()]),
    _view("preparedSettlementBatchId", [], [_uint()]),
    _view("totalPendingDepositAssets", [], [_uint()]),
    _view("totalPendingWithdrawalShares", [], [_uint()]),
    _view("reservedWithdrawalAssets", [], [_uint()]),
    _view("accountedUnderlyingAssets", [], [_uint()]),
    _view("availableUnderlyingAssets", [], [_uint()]),
    _view("reservedUnderlyingAssets", [], [_uint()]),
    _view("allocatedUnderlyingAssets", [], [_uint()]),
    _view("cumulativeUnderlyingPerShare", [], [_uint()]),
    _view("currentShareGeneration", [], [_uint()]),
    _view("batchCount", [], [_uint()]),
    _view("settlementDefaultDelay", [], [_uint()]),
    _view("totalPendingWithdrawalClaims", [], [_uint()]),
    _view(
        "epochs",
        [_uint("epochId")],
        [
            {"name": "startedAt", "type": "uint64"},
            {"name": "endedAt", "type": "uint64"},
            _uint("deposits"),
            _uint("withdrawals"),
            _uint("committedCollateral"),
            _uint("returnedCollateral"),
            _uint("premiumEarned"),
            _uint("assignmentShortfall"),
            _uint("performanceFee"),
            _uint("withdrawalAssetsPerShare"),
            _uint("withdrawalAssetsRemaining"),
            _uint("remainingWithdrawalClaims"),
            {"name": "closed", "type": "bool"},
        ],
    ),
    _view(
        "batches",
        [_uint("batchId")],
        [
            _uint("epochId"),
            _address("oToken"),
            _uint("protocolVaultId"),
            _uint("amount"),
            _uint("collateral"),
            _uint("premiumEarned"),
            _uint("collateralReturned"),
            {"name": "settled", "type": "bool"},
        ],
    ),
    _view("batchUnderlyingReceived", [_uint("batchId")], [_uint()]),
    _view("sharesOf", [_address("user")], [_uint()]),
    _view("pendingDepositAssets", [_address("user")], [_uint()]),
    _view("pendingWithdrawalShares", [_address("user")], [_uint()]),
    _view("pendingWithdrawalEpoch", [_address("user")], [_uint()]),
    _view("claimableAssignedUnderlying", [_address("user")], [_uint()]),
    _view("shareGeneration", [_address("user")], [_uint()]),
    _view("underlyingPerSharePaid", [_address("user")], [_uint()]),
    _view("withdrawalUnderlyingPerShare", [_uint("epochId")], [_uint()]),
    _view("withdrawalUnderlyingRemaining", [_uint("epochId")], [_uint()]),
    _view(
        "generationCumulativeUnderlyingPerShare",
        [_uint("generation")],
        [_uint()],
    ),
]


OTOKEN_METADATA_ABI = [
    _view("underlying", [], [_address()]),
    _view("strikeAsset", [], [_address()]),
    _view("collateralAsset", [], [_address()]),
    _view("strikePrice", [], [_uint()]),
    _view("expiry", [], [_uint()]),
    _view("isPut", [], [{"name": "", "type": "bool"}]),
]


MULTICALL3_ABI = [
    {
        "type": "function",
        "name": "tryBlockAndAggregate",
        "inputs": [
            {"name": "requireSuccess", "type": "bool"},
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "callData", "type": "bytes"},
                ],
            },
        ],
        "outputs": [
            {"name": "blockNumber", "type": "uint256"},
            {"name": "blockHash", "type": "bytes32"},
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            },
        ],
        "stateMutability": "payable",
    }
]
