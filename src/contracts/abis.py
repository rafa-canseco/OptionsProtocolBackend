"""
Minimal ABIs — only the functions/events the backend actually calls.

These are derived from the Foundry-compiled contracts in blockchain/out/.
If the contract instance updates function signatures or event names,
update these ABIs to match.
"""

BATCH_SETTLER_ABI = [
    # OrderExecuted — emitted by executeOrder().
    # NOTE: if the contracts instance renames this event, update here.
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": True, "name": "oToken", "type": "address"},
            {"indexed": True, "name": "mm", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "grossPremium", "type": "uint256"},
            {"indexed": False, "name": "netPremium", "type": "uint256"},
            {"indexed": False, "name": "fee", "type": "uint256"},
            {"indexed": False, "name": "collateral", "type": "uint256"},
            {"indexed": False, "name": "vaultId", "type": "uint256"},
        ],
        "name": "OrderExecuted",
        "type": "event",
    },
    # batchSettleVaults(address[], uint256[]) — post-expiry settlement
    {
        "inputs": [
            {"name": "owners", "type": "address[]"},
            {"name": "vaultIds", "type": "uint256[]"},
        ],
        "name": "batchSettleVaults",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # batchRedeem(address[], uint256[])
    {
        "inputs": [
            {"name": "oTokens", "type": "address[]"},
            {"name": "amounts", "type": "uint256[]"},
        ],
        "name": "batchRedeem",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # physicalRedeem(address oToken, address user, uint256 amount, uint256 maxCollateralSpent, address mm)
    # Executes physical delivery for ITM positions via flash loan + DEX swap.
    {
        "inputs": [
            {"name": "oToken", "type": "address"},
            {"name": "user", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "maxCollateralSpent", "type": "uint256"},
            {"name": "mm", "type": "address"},
        ],
        "name": "physicalRedeem",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # PhysicalDelivery — emitted by physicalRedeem().
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "oToken", "type": "address"},
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "contraAmount", "type": "uint256"},
            {"indexed": False, "name": "collateralUsed", "type": "uint256"},
        ],
        "name": "PhysicalDelivery",
        "type": "event",
    },
    # batchPhysicalRedeem(address[], address[], uint256[], uint256[], address[])
    {
        "inputs": [
            {"name": "oTokens", "type": "address[]"},
            {"name": "users", "type": "address[]"},
            {"name": "amounts", "type": "uint256[]"},
            {"name": "maxCollateralSpents", "type": "uint256[]"},
            {"name": "mms", "type": "address[]"},
        ],
        "name": "batchPhysicalRedeem",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # makerNonce(address) → uint256 — current nonce for a market maker
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "makerNonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # incrementMakerNonce() — invalidates all outstanding quotes for msg.sender
    {
        "inputs": [],
        "name": "incrementMakerNonce",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

OTOKEN_FACTORY_ABI = [
    {
        "inputs": [],
        "name": "getOTokensLength",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "", "type": "uint256"}],
        "name": "oTokens",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    # getOToken(bytes32) → address — mapping getter, returns address(0) if not created
    {
        "inputs": [{"name": "", "type": "bytes32"}],
        "name": "getOToken",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    # createOToken(address,address,address,uint256,uint256,bool) → address
    {
        "inputs": [
            {"name": "_underlying", "type": "address"},
            {"name": "_strikeAsset", "type": "address"},
            {"name": "_collateralAsset", "type": "address"},
            {"name": "_strikePrice", "type": "uint256"},
            {"name": "_expiry", "type": "uint256"},
            {"name": "_isPut", "type": "bool"},
        ],
        "name": "createOToken",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # isOToken(address) → bool — check if an address is a factory-created oToken
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "isOToken",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    # getTargetOTokenAddress — deterministic CREATE2 address (no storage read)
    {
        "inputs": [
            {"name": "_underlying", "type": "address"},
            {"name": "_strikeAsset", "type": "address"},
            {"name": "_collateralAsset", "type": "address"},
            {"name": "_strikePrice", "type": "uint256"},
            {"name": "_expiry", "type": "uint256"},
            {"name": "_isPut", "type": "bool"},
        ],
        "name": "getTargetOTokenAddress",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

OTOKEN_ABI = [
    {
        "inputs": [],
        "name": "strikePrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "expiry",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "isPut",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "collateralAsset",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "underlying",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ORACLE_ABI = [
    # getExpiryPrice(address asset, uint256 expiry) → (uint256 price, bool isFinalized)
    {
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_expiryTimestamp", "type": "uint256"},
        ],
        "name": "getExpiryPrice",
        "outputs": [
            {"name": "", "type": "uint256"},
            {"name": "", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # setExpiryPrice(address asset, uint256 expiry, uint256 price) — owner or operator
    {
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_expiry", "type": "uint256"},
            {"name": "_price", "type": "uint256"},
        ],
        "name": "setExpiryPrice",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # transferOwnership(address newOwner) — 2-step, step 1
    {
        "inputs": [{"name": "newOwner", "type": "address"}],
        "name": "transferOwnership",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # acceptOwnership() — 2-step, step 2
    {
        "inputs": [],
        "name": "acceptOwnership",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # owner() — read current owner
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

CONTROLLER_ABI = [
    # getVault(address owner, uint256 vaultId) → Vault tuple
    {
        "inputs": [
            {"name": "_accountOwner", "type": "address"},
            {"name": "_vaultId", "type": "uint256"},
        ],
        "name": "getVault",
        "outputs": [
            {
                "components": [
                    {"name": "shortOtoken", "type": "address"},
                    {"name": "collateralAsset", "type": "address"},
                    {"name": "shortAmount", "type": "uint256"},
                    {"name": "collateralAmount", "type": "uint256"},
                ],
                "name": "",
                "type": "tuple",
            },
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # vaultSettled(address owner, uint256 vaultId) → bool
    {
        "inputs": [
            {"name": "", "type": "address"},
            {"name": "", "type": "uint256"},
        ],
        "name": "vaultSettled",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

WHITELIST_ABI = [
    # whitelistOToken(address) — owner only
    {
        "inputs": [{"name": "_oToken", "type": "address"}],
        "name": "whitelistOToken",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # isWhitelistedOToken(address) → bool
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "isWhitelistedOToken",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

MOCK_ERC20_MINT_ABI = [
    # mint(address to, uint256 amount) — permissionless on MockERC20
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mint",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

UNISWAP_V3_QUOTER_ABI = [
    # quoteExactOutputSingle — estimate how much input is needed for exact output
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            },
        ],
        "name": "quoteExactOutputSingle",
        "outputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# --- Yield tracking ABIs ---

CONTROLLER_YIELD_EVENTS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "owner", "type": "address"},
            {"indexed": False, "name": "vaultId", "type": "uint256"},
            {"indexed": False, "name": "asset", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "CollateralDeposited",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "owner", "type": "address"},
            {"indexed": False, "name": "vaultId", "type": "uint256"},
            {"indexed": False, "name": "collateralReturned", "type": "uint256"},
        ],
        "name": "VaultSettled",
        "type": "event",
    },
]

MARGIN_POOL_YIELD_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "asset", "type": "address"},
            {"indexed": False, "name": "recipient", "type": "address"},
            {"indexed": False, "name": "yield", "type": "uint256"},
        ],
        "name": "YieldHarvested",
        "type": "event",
    },
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "harvestYield",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getAccruedYield",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_TRANSFER_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
