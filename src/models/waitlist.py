from pydantic import BaseModel, EmailStr, Field, field_validator


class WaitlistRequest(BaseModel):
    email: EmailStr = Field(description="Email address to add to waitlist", examples=["user@example.com"])

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()


class WaitlistResponse(BaseModel):
    ok: bool = Field(description="Whether the request succeeded", examples=[True])
    new: bool = Field(description="True if this email was added for the first time, false if already on the list", examples=[True])
