"""Versioned fair-value policy for European ETH/USDC cash-secured puts."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from src.pricing.black_scholes import OptionType, price

MODEL_VERSION = 1
MODEL_NAME = "b1nary-european-bs-put-v1"
METHODOLOGY = "european_black_scholes"
SOURCE_QUALITY = "single_model_multi_signer"
UINT192_MAX = 2**192 - 1


@dataclass(frozen=True, slots=True)
class FairValuePolicy:
    implied_volatility_bps: int
    implied_volatility_source: str
    risk_free_rate_bps: int
    settlement_cost_bps: int
    model_name: str = MODEL_NAME
    model_version: int = MODEL_VERSION

    def __post_init__(self) -> None:
        if self.model_name != MODEL_NAME or self.model_version != MODEL_VERSION:
            raise ValueError("UNSUPPORTED_FAIR_VALUE_MODEL")
        if not 1 <= self.implied_volatility_bps <= 50_000:
            raise ValueError("FAIR_VALUE_IV_BPS_OUT_OF_RANGE")
        if not self.implied_volatility_source.strip():
            raise ValueError("FAIR_VALUE_IV_SOURCE_REQUIRED")
        if not -1_000 <= self.risk_free_rate_bps <= 5_000:
            raise ValueError("FAIR_VALUE_RISK_FREE_RATE_BPS_OUT_OF_RANGE")
        if not 0 <= self.settlement_cost_bps <= 2_000:
            raise ValueError("FAIR_VALUE_SETTLEMENT_COST_BPS_OUT_OF_RANGE")


@dataclass(frozen=True, slots=True)
class EuropeanPutInputs:
    spot_price_8: int
    strike_price_8: int
    option_amount_8: int
    expiry_timestamp: int
    snapshot_timestamp: int
    collateral_assets: int
    accounting_asset_decimals: int

    def __post_init__(self) -> None:
        if self.spot_price_8 <= 0 or self.strike_price_8 <= 0:
            raise ValueError("INVALID_FAIR_VALUE_PRICE")
        if self.option_amount_8 <= 0 or self.collateral_assets <= 0:
            raise ValueError("INVALID_FAIR_VALUE_POSITION")
        if not 0 <= self.accounting_asset_decimals <= 18:
            raise ValueError("INVALID_ACCOUNTING_ASSET_DECIMALS")


@dataclass(frozen=True, slots=True)
class EuropeanPutMark:
    fair_liability_assets: int
    stress_liability_assets: int
    settlement_cost_assets: int
    option_price_8: int
    time_to_expiry_seconds: int


def versioned_observation_nonce(sequence: int) -> int:
    """Bind an observation nonce to the model version without changing the ABI."""
    if not 1 <= sequence <= UINT192_MAX:
        raise ValueError("FAIR_VALUE_NONCE_SEQUENCE_OUT_OF_RANGE")
    return (MODEL_VERSION << 192) | sequence


def observation_model_version(nonce: int) -> int:
    if not 0 <= nonce < 2**256:
        raise ValueError("FAIR_VALUE_NONCE_OUT_OF_RANGE")
    return nonce >> 192


def mark_european_put(
    inputs: EuropeanPutInputs, policy: FairValuePolicy
) -> EuropeanPutMark:
    """Return fair and stress liabilities in accounting-asset base units.

    The executable product cannot be closed early, but its European expiry
    obligation still has a time value. Black-Scholes supplies that fair,
    non-executable mark. Full collateral is retained only as a stress metric.
    """
    seconds = max(inputs.expiry_timestamp - inputs.snapshot_timestamp, 0)
    spot = inputs.spot_price_8 / 1e8
    strike = inputs.strike_price_8 / 1e8
    option_price = price(
        OptionType.PUT,
        spot,
        strike,
        seconds / (365 * 24 * 60 * 60),
        policy.risk_free_rate_bps / 10_000,
        policy.implied_volatility_bps / 10_000,
    )
    option_price_8 = max(
        0,
        int(
            (Decimal(str(option_price)) * Decimal(10**8)).to_integral_value(
                rounding=ROUND_CEILING
            )
        ),
    )
    scale = Decimal(10**inputs.accounting_asset_decimals)
    fair = int(
        (
            Decimal(option_price_8)
            * Decimal(inputs.option_amount_8)
            * scale
            / Decimal(10**16)
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    fair = min(fair, inputs.collateral_assets)
    settlement_cost = int(
        (
            Decimal(fair) * Decimal(policy.settlement_cost_bps) / Decimal(10_000)
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    return EuropeanPutMark(
        fair_liability_assets=fair,
        stress_liability_assets=inputs.collateral_assets,
        settlement_cost_assets=settlement_cost,
        option_price_8=option_price_8,
        time_to_expiry_seconds=seconds,
    )
