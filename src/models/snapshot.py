import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.models.snapshot_state import bytes32, evm_address, validate_snapshot_state

_SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}")


class StrictSnapshotModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class MarketMakerSnapshot(StrictSnapshotModel):
    mm_address: str
    usdc_address: str
    allowance_spender: str
    usdc_balance_raw: int = Field(ge=0)
    usdc_allowance_raw: int = Field(ge=0)
    maker_nonce: int = Field(ge=0)

    @field_validator("mm_address", "usdc_address", "allowance_spender")
    @classmethod
    def valid_addresses(cls, value: str) -> str:
        return evm_address(value)


class SnapshotOToken(StrictSnapshotModel):
    address: str
    strike_price: float = Field(gt=0)
    expiry: int = Field(gt=0)
    is_put: bool

    @field_validator("address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        return evm_address(value)


class SnapshotMarket(StrictSnapshotModel):
    asset: str = Field(min_length=1)
    spot: float = Field(gt=0)
    iv: float = Field(ge=0)
    iv_source: str = Field(min_length=1)
    observed_at: int = Field(ge=0)
    protocol_fee_bps: int = Field(ge=0, le=10_000)
    available_otokens: list[SnapshotOToken]


class SnapshotQuote(StrictSnapshotModel):
    asset: str = Field(min_length=1)
    chain: str = Field(min_length=1)
    is_put: bool
    created_at: int = Field(ge=0)
    deadline: int = Field(gt=0)
    expiry: int = Field(gt=0)
    strike_price: float = Field(gt=0)
    deployment_status: Literal["virtual", "creating", "ready", "failed"]
    otoken_address: str
    bid_price: int = Field(ge=0)
    quote_id: int = Field(ge=0)
    max_amount: int = Field(gt=0)
    maker_nonce: int = Field(ge=0)
    signature: str

    @field_validator("otoken_address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        return evm_address(value)

    @field_validator("signature")
    @classmethod
    def valid_signature(cls, value: str) -> str:
        if _SIGNATURE.fullmatch(value) is None:
            raise ValueError("Expected a 65-byte signature")
        return value


class SnapshotCommon(StrictSnapshotModel):
    market: SnapshotMarket
    quotes: list[SnapshotQuote]
    market_maker: MarketMakerSnapshot


class SnapshotFund(StrictSnapshotModel):
    fund_key: str = Field(min_length=1)
    fund_type: Literal["csp", "covered_call", "meta_wheel"]
    fund_address: str
    state: dict[str, Any]

    @field_validator("fund_address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        return evm_address(value)

    @model_validator(mode="after")
    def strict_state_schema(self):
        validate_snapshot_state(self.fund_type, self.state)
        return self


class SnapshotEnvelope(StrictSnapshotModel):
    environment: str = Field(min_length=1)
    chain_id: int = Field(gt=0)
    window_id: int = Field(gt=0)
    generation: int = Field(gt=0)
    snapshot_block: int = Field(gt=0)
    snapshot_block_hash: str
    snapshot_block_timestamp: int = Field(ge=0)
    published_at: str
    chain_data_age_seconds: float = Field(ge=0)
    reconciled: bool
    stale: bool
    common: SnapshotCommon
    funds: list[SnapshotFund] = Field(min_length=1, max_length=3)

    @field_validator("snapshot_block_hash")
    @classmethod
    def valid_block_hash(cls, value: str) -> str:
        return bytes32(value)

    @field_validator("published_at")
    @classmethod
    def timezone_aware_published_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("published_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        return value

    @model_validator(mode="after")
    def unique_fund_types(self):
        fund_types = [fund.fund_type for fund in self.funds]
        if len(fund_types) != len(set(fund_types)):
            raise ValueError("fund_type values must be unique")
        return self
