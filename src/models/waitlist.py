from pydantic import BaseModel, EmailStr, field_validator


class WaitlistRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()


class WaitlistResponse(BaseModel):
    ok: bool
    new: bool
