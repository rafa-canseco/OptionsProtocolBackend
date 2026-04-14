"""Asset registry for multi-asset, multi-chain support.

Each supported underlying has a config entry defining its chain,
oracle source, strike generation params, and token addresses.
"""

from dataclasses import dataclass
from enum import Enum

from src.chains import Chain
from src.config import settings


class Asset(str, Enum):
    # Base
    ETH = "eth"
    BTC = "btc"
    # Solana
    SOL = "sol"
    # XLayer
    OKB = "okb"


@dataclass(frozen=True)
class AssetConfig:
    symbol: str
    chain: Chain
    decimals: int
    strike_step: float
    short_expiry_strike_step: float
    num_strikes: int
    deribit_index: str = ""
    deribit_currency: str = ""
    min_otm_per_side: int = 4

    @property
    def has_deribit(self) -> bool:
        return bool(self.deribit_index)

    @property
    def chainlink_feed_address(self) -> str:
        if self.chain == Chain.SOLANA:
            raise ValueError(f"{self.symbol} is on Solana. Use Pyth.")
        if self.symbol == "ETH":
            return settings.chainlink_eth_usd_address
        if self.symbol == "BTC":
            return settings.chainlink_btc_usd_address
        if self.symbol == "OKB":
            return settings.chainlink_okb_usd_address
        raise ValueError(f"No Chainlink feed for {self.symbol}")

    @property
    def underlying_address(self) -> str:
        """Token address on the asset's native chain."""
        if self.symbol == "ETH":
            return settings.weth_address
        if self.symbol == "BTC":
            return settings.wbtc_address
        if self.symbol == "SOL":
            return settings.solana_wsol_mint
        if self.symbol == "OKB":
            return settings.wokb_address
        raise ValueError(f"No underlying address for {self.symbol}")

    @property
    def pyth_feed_id(self) -> str:
        if self.chain != Chain.SOLANA:
            raise ValueError(
                f"{self.symbol} is on {self.chain.value}, not Solana. "
                "Use Chainlink for Base assets."
            )
        feed_id = _PYTH_FEED_IDS.get(self.symbol)
        if feed_id is None:
            raise ValueError(
                f"No Pyth feed ID in _PYTH_FEED_IDS for {self.symbol}. "
                "Add it to the dict in assets.py."
            )
        return feed_id


# Pyth feed IDs for Solana assets (from CONTEXT.md devnet config)
_PYTH_FEED_IDS: dict[str, str] = {
    "SOL": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
}


ASSET_CONFIGS: dict[Asset, AssetConfig] = {
    # ── Base ──
    Asset.ETH: AssetConfig(
        symbol="ETH",
        chain=Chain.BASE,
        decimals=18,
        strike_step=50.0,
        short_expiry_strike_step=25.0,
        num_strikes=5,
        deribit_index="eth_usd",
        deribit_currency="ETH",
    ),
    Asset.BTC: AssetConfig(
        symbol="BTC",
        chain=Chain.BASE,
        decimals=8,
        strike_step=1000.0,
        short_expiry_strike_step=500.0,
        num_strikes=5,
        deribit_index="btc_usd",
        deribit_currency="BTC",
    ),
    # ── Solana ──
    Asset.SOL: AssetConfig(
        symbol="SOL",
        chain=Chain.SOLANA,
        decimals=9,
        strike_step=1.0,
        short_expiry_strike_step=1.0,
        num_strikes=5,
        deribit_index="sol_usdc",
        deribit_currency="USDC",
    ),
    # ── XLayer ──
    Asset.OKB: AssetConfig(
        symbol="OKB",
        chain=Chain.XLAYER,
        decimals=18,
        strike_step=2.0,
        short_expiry_strike_step=1.0,
        num_strikes=5,
    ),
}


def get_asset_config(asset: Asset) -> AssetConfig:
    try:
        return ASSET_CONFIGS[asset]
    except KeyError:
        supported = ", ".join(a.value for a in ASSET_CONFIGS)
        raise ValueError(
            f"Unsupported asset {asset!r}. Supported: {supported}"
        ) from None


def get_chain_for_asset(asset: Asset) -> Chain:
    return get_asset_config(asset).chain


def get_base_assets() -> list[Asset]:
    return [a for a, c in ASSET_CONFIGS.items() if c.chain == Chain.BASE]


def get_solana_assets() -> list[Asset]:
    return [a for a, c in ASSET_CONFIGS.items() if c.chain == Chain.SOLANA]


def get_xlayer_assets() -> list[Asset]:
    return [a for a, c in ASSET_CONFIGS.items() if c.chain == Chain.XLAYER]
