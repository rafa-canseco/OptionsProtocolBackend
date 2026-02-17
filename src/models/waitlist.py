from pydantic import BaseModel, EmailStr


class WaitlistRequest(BaseModel):
    email: EmailStr


class WaitlistResponse(BaseModel):
    ok: bool
