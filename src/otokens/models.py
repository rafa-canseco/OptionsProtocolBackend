"""Canonical oToken series identity."""

from dataclasses import dataclass

from eth_abi import encode
from web3 import Web3


@dataclass(frozen=True)
class CanonicalSeries:
    chain_id: int
    factory_address: str
    underlying: str
    strike_asset: str
    collateral_asset: str
    strike_price_raw: int
    expiry: int
    is_put: bool

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if self.strike_price_raw <= 0:
            raise ValueError("strike_price_raw must be positive")
        if self.expiry <= 0:
            raise ValueError("expiry must be positive")
        for field in (
            "factory_address",
            "underlying",
            "strike_asset",
            "collateral_asset",
        ):
            object.__setattr__(
                self,
                field,
                Web3.to_checksum_address(getattr(self, field)),
            )

    @property
    def series_key(self) -> str:
        """Deployment-scoped canonical identity used by the DB lease."""
        encoded = encode(
            [
                "uint256",
                "address",
                "address",
                "address",
                "address",
                "uint256",
                "uint256",
                "bool",
            ],
            [
                self.chain_id,
                Web3.to_checksum_address(self.factory_address),
                Web3.to_checksum_address(self.underlying),
                Web3.to_checksum_address(self.strike_asset),
                Web3.to_checksum_address(self.collateral_asset),
                self.strike_price_raw,
                self.expiry,
                self.is_put,
            ],
        )
        return Web3.keccak(encoded).hex()

    @property
    def factory_args(self) -> tuple:
        return (
            Web3.to_checksum_address(self.underlying),
            Web3.to_checksum_address(self.strike_asset),
            Web3.to_checksum_address(self.collateral_asset),
            self.strike_price_raw,
            self.expiry,
            self.is_put,
        )

    def to_row(
        self,
        *,
        otoken_address: str,
        strike_price: float,
        deployment_status: str,
    ) -> dict:
        return {
            "chain_id": self.chain_id,
            "series_key": self.series_key,
            "factory_address": self.factory_address.lower(),
            "otoken_address": otoken_address.lower(),
            "underlying": self.underlying.lower(),
            "strike_asset": self.strike_asset.lower(),
            "collateral_asset": self.collateral_asset.lower(),
            "strike_price": strike_price,
            "strike_price_raw": str(self.strike_price_raw),
            "expiry": self.expiry,
            "is_put": self.is_put,
            "chain": "base",
            "deployment_status": deployment_status,
        }


def canonical_series_from_row(row: dict) -> CanonicalSeries:
    required = (
        "factory_address",
        "chain_id",
        "underlying",
        "strike_asset",
        "collateral_asset",
        "strike_price_raw",
        "expiry",
        "is_put",
    )
    missing = [key for key in required if row.get(key) is None]
    if missing:
        raise ValueError(f"series row missing canonical fields: {', '.join(missing)}")
    return CanonicalSeries(
        chain_id=int(row["chain_id"]),
        factory_address=str(row["factory_address"]),
        underlying=str(row["underlying"]),
        strike_asset=str(row["strike_asset"]),
        collateral_asset=str(row["collateral_asset"]),
        strike_price_raw=int(row["strike_price_raw"]),
        expiry=int(row["expiry"]),
        is_put=bool(row["is_put"]),
    )
