"""Asset registry for multi-asset support.

Each supported underlying has a config entry defining its
Chainlink feed, Deribit identifiers, and strike generation params.
"""

from dataclasses import dataclass
from enum import Enum

from src.config import settings


class Asset(str, Enum):
    ETH = "eth"
    BTC = "btc"


@dataclass(frozen=True)
class AssetConfig:
    symbol: str
    deribit_index: str
    deribit_currency: str
    strike_step: float
    num_strikes: int

    @property
    def chainlink_feed_address(self) -> str:
        if self.symbol == "ETH":
            return settings.chainlink_eth_usd_address
        if self.symbol == "BTC":
            return settings.chainlink_btc_usd_address
        raise ValueError(f"No Chainlink feed for {self.symbol}")

    @property
    def underlying_address(self) -> str:
        if self.symbol == "ETH":
            return settings.weth_address
        if self.symbol == "BTC":
            return settings.wbtc_address
        raise ValueError(f"No underlying address for {self.symbol}")


ASSET_CONFIGS: dict[Asset, AssetConfig] = {
    Asset.ETH: AssetConfig(
        symbol="ETH",
        deribit_index="eth_usd",
        deribit_currency="ETH",
        strike_step=50.0,
        num_strikes=5,
    ),
    Asset.BTC: AssetConfig(
        symbol="BTC",
        deribit_index="btc_usd",
        deribit_currency="BTC",
        strike_step=1000.0,
        num_strikes=5,
    ),
}


def get_asset_config(asset: Asset) -> AssetConfig:
    return ASSET_CONFIGS[asset]
