"""
Minimal ABIs — only the functions/events the backend actually calls.

These are derived from the Foundry-compiled contracts in blockchain/out/.
If the contract instance updates function signatures or event names,
update these ABIs to match.
"""

PRICE_SHEET_ABI = [
    # publishQuotes(address[], uint256[], uint256[], uint256[], uint256[])
    {
        "inputs": [
            {"name": "oTokens", "type": "address[]"},
            {"name": "bidPrices", "type": "uint256[]"},
            {"name": "askPrices", "type": "uint256[]"},
            {"name": "deadlines", "type": "uint256[]"},
            {"name": "maxAmounts", "type": "uint256[]"},
        ],
        "name": "publishQuotes",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # invalidateQuotes(address[])
    {
        "inputs": [{"name": "oTokens", "type": "address[]"}],
        "name": "invalidateQuotes",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # getQuote(address) → (bidPrice, askPrice, maxAmount, filledAmount, isValid)
    {
        "inputs": [{"name": "oToken", "type": "address"}],
        "name": "getQuote",
        "outputs": [
            {"name": "bidPrice", "type": "uint256"},
            {"name": "askPrice", "type": "uint256"},
            {"name": "maxAmount", "type": "uint256"},
            {"name": "filledAmount", "type": "uint256"},
            {"name": "isValid", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # Events
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "oToken", "type": "address"},
            {"indexed": False, "name": "bidPrice", "type": "uint256"},
            {"indexed": False, "name": "askPrice", "type": "uint256"},
            {"indexed": False, "name": "deadline", "type": "uint256"},
            {"indexed": False, "name": "maxAmount", "type": "uint256"},
        ],
        "name": "QuotePublished",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [{"indexed": True, "name": "oToken", "type": "address"}],
        "name": "QuoteInvalidated",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "oToken", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "newFilledAmount", "type": "uint256"},
        ],
        "name": "QuoteFilled",
        "type": "event",
    },
]

BATCH_SETTLER_ABI = [
    # OrderExecuted — emitted by executeOrder().
    # NOTE: if the contracts instance renames this event, update here.
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": True, "name": "oToken", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "premium", "type": "uint256"},
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
]
