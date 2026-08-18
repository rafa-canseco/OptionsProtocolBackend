"""Wire models for lazy oToken series materialization."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

UINT256_MAX = 2**256 - 1
DECIMAL_UINT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")


def parse_uint256(value: str, field_name: str) -> int:
    if not DECIMAL_UINT_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be an unsigned decimal string")
    parsed = int(value)
    if parsed > UINT256_MAX:
        raise ValueError(f"{field_name} exceeds uint256")
    return parsed


class ExecutionQuoteSnapshot(BaseModel):
    """Complete immutable EIP-712 quote snapshot reviewed by the user."""

    otoken_address: str
    bid_price_raw: str
    deadline: str
    quote_id: str
    max_amount_raw: str
    maker_nonce: str
    signature: str
    mm_address: str

    @field_validator("otoken_address", "mm_address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if not EVM_ADDRESS_RE.fullmatch(value):
            raise ValueError("must be a 0x-prefixed EVM address")
        return value

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        if not SIGNATURE_RE.fullmatch(value):
            raise ValueError("must be a 65-byte 0x-prefixed signature")
        return value

    @field_validator(
        "bid_price_raw",
        "deadline",
        "quote_id",
        "max_amount_raw",
        "maker_nonce",
    )
    @classmethod
    def validate_uint256(cls, value: str, info) -> str:
        parse_uint256(value, info.field_name)
        return value

    def as_ints(self) -> dict[str, int]:
        return {
            "bid_price": parse_uint256(self.bid_price_raw, "bid_price_raw"),
            "deadline": parse_uint256(self.deadline, "deadline"),
            "quote_id": parse_uint256(self.quote_id, "quote_id"),
            "max_amount": parse_uint256(self.max_amount_raw, "max_amount_raw"),
            "maker_nonce": parse_uint256(self.maker_nonce, "maker_nonce"),
        }


class EnsureSeriesRequest(BaseModel):
    wallet_address: str
    expected_otoken_address: str
    amount_raw: str
    quote: ExecutionQuoteSnapshot

    @field_validator("wallet_address", "expected_otoken_address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if not EVM_ADDRESS_RE.fullmatch(value):
            raise ValueError("must be a 0x-prefixed EVM address")
        return value

    @field_validator("amount_raw")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        if parse_uint256(value, "amount_raw") == 0:
            raise ValueError("amount_raw must be greater than zero")
        return value

    @model_validator(mode="after")
    def validate_quote_target(self) -> "EnsureSeriesRequest":
        if self.expected_otoken_address.lower() != self.quote.otoken_address.lower():
            raise ValueError("expected_otoken_address must match quote.otoken_address")
        return self


class EnsureFundSeriesRequest(BaseModel):
    """Internal fund intent for one policy-selected virtual series."""

    adapter_address: str
    expected_otoken_address: str
    amount_raw: str
    quote: ExecutionQuoteSnapshot

    @field_validator("adapter_address", "expected_otoken_address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if not EVM_ADDRESS_RE.fullmatch(value):
            raise ValueError("must be a 0x-prefixed EVM address")
        return value

    @field_validator("amount_raw")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        if parse_uint256(value, "amount_raw") == 0:
            raise ValueError("amount_raw must be greater than zero")
        return value

    @model_validator(mode="after")
    def validate_quote_target(self) -> "EnsureFundSeriesRequest":
        if self.expected_otoken_address.lower() != self.quote.otoken_address.lower():
            raise ValueError("expected_otoken_address must match quote.otoken_address")
        return self


class EnsureSeriesResponse(BaseModel):
    status: Literal["ready", "creating"]
    otoken_address: str
    execution_quote: ExecutionQuoteSnapshot
    retry_after_ms: int | None = Field(default=None, ge=1)
    deployment_tx_hash: str | None = None


class SeriesErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
