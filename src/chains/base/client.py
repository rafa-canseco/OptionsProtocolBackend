"""Base chain client — thin re-export layer.

Existing code in src/contracts/ and src/pricing/ is the real
implementation. This module provides a consistent import path
so downstream code can use:

    from src.chains.base.client import get_spot_price, get_balance
    from src.chains.solana.client import get_spot_price, get_balance

without the caller needing to know chain internals.
"""

import logging

from web3 import Web3

from src.contracts.web3_client import get_w3
from src.pricing.assets import Asset
from src.pricing.chainlink import get_asset_price

logger = logging.getLogger(__name__)

ERC20_BALANCE_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def get_spot_price(asset: Asset) -> tuple[float, int]:
    """Read USD spot price from Chainlink. Returns (price, updated_at)."""
    return get_asset_price(asset)


def get_balance(owner: str, token_address: str) -> int:
    """Read ERC-20 token balance for owner. Returns raw amount."""
    w3 = get_w3()
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_BALANCE_ABI,
    )
    return contract.functions.balanceOf(Web3.to_checksum_address(owner)).call()


def get_eth_balance(owner: str) -> int:
    """Read native ETH balance for owner. Returns wei."""
    w3 = get_w3()
    return w3.eth.get_balance(Web3.to_checksum_address(owner))
