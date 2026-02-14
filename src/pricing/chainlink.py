from web3 import Web3

from src.config import settings

AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def get_eth_price() -> float:
    """Read ETH/USD price from Chainlink on Base Sepolia."""
    w3 = Web3(Web3.HTTPProvider(settings.base_sepolia_rpc_url))
    feed = w3.eth.contract(
        address=Web3.to_checksum_address(settings.chainlink_eth_usd_address),
        abi=AGGREGATOR_V3_ABI,
    )
    decimals = feed.functions.decimals().call()
    (_, answer, _, updated_at, _) = feed.functions.latestRoundData().call()
    return answer / (10**decimals), updated_at
