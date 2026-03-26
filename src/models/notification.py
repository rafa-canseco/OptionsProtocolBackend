import re

from pydantic import BaseModel, EmailStr, Field, field_validator

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _validate_wallet(v: str) -> str:
    if not ETH_ADDRESS_RE.match(v):
        raise ValueError("Invalid Ethereum address")
    return v.lower()


class EmailSubmitRequest(BaseModel):
    wallet_address: str = Field(
        description="Ethereum wallet address",
        examples=["0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"],
    )
    email: EmailStr = Field(
        description="Email address for notifications",
        examples=["user@example.com"],
    )

    @field_validator("wallet_address")
    @classmethod
    def validate_wallet(cls, v: str) -> str:
        return _validate_wallet(v)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()


class EmailVerifyRequest(BaseModel):
    wallet_address: str = Field(
        description="Ethereum wallet address",
        examples=["0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"],
    )
    code: str = Field(
        description="6-digit verification code",
        examples=["384921"],
    )

    @field_validator("wallet_address")
    @classmethod
    def validate_wallet(cls, v: str) -> str:
        return _validate_wallet(v)

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        if not re.match(r"^\d{6}$", v):
            raise ValueError("Code must be exactly 6 digits")
        return v


class NotificationStatusResponse(BaseModel):
    has_email: bool = Field(description="Whether wallet has registered an email")
    verified: bool = Field(description="Whether the email is verified")
    unsubscribed: bool = Field(description="Whether notifications are disabled")
