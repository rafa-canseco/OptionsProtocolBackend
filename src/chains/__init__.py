"""Chain abstraction layer.

Each supported blockchain has its own submodule (base/, solana/) with
chain-specific RPC, signing, oracle, and event parsing logic.

The chain for a given request is determined by the asset: ETH/BTC
route to Base, SOL/TSLAX route to Solana.
"""

from enum import Enum


class Chain(str, Enum):
    ARC = "arc"
    BASE = "base"
    SOLANA = "solana"
