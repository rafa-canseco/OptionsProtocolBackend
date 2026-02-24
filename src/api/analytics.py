import logging
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["Analytics"])

EVENT_TYPES = Literal[
    "slider_use", "signup", "first_trade",
    "return_visit", "share_result", "settle",
]


class SliderInteraction(BaseModel):
    session_id: str = Field(description="Unique session identifier from the frontend", examples=["sess_abc123"])
    selected_price: float = Field(description="Strike price the user selected on the slider (USD)", examples=[2400.0])
    side: str = Field(default="buy", description="Side of the trade shown (currently always 'buy')", examples=["buy"])
    shown_premium: float | None = Field(default=None, description="Premium displayed to the user at time of interaction (USD)", examples=[42.15])
    converted_to_signup: bool = Field(default=False, description="Whether this slider interaction led to a waitlist signup", examples=[False])


class EngagementEvent(BaseModel):
    user_address: str | None = Field(default=None, description="Ethereum address of the user (if connected)", examples=["0xabcdef0123456789abcdef0123456789abcdef01"])
    event_type: EVENT_TYPES = Field(description="Type of engagement event", examples=["signup"])
    metadata: dict = Field(default={}, description="Arbitrary key-value metadata for the event", examples=[{"source": "landing_page"}])


@router.post("/slider", status_code=202, summary="Log slider interaction")
async def log_slider(body: SliderInteraction):
    """Record a landing-page slider interaction for analytics.

    Fire-and-forget — always returns 202. DB errors are logged server-side
    but never surfaced to the caller.
    """
    try:
        client = get_client()
        client.table("slider_interactions").insert({
            "session_id": body.session_id,
            "selected_price": body.selected_price,
            "side": body.side,
            "shown_premium": body.shown_premium,
            "converted_to_signup": body.converted_to_signup,
        }).execute()
    except Exception:
        logger.error("Failed to log slider interaction", exc_info=True)

    return {"ok": True}


@router.post("/event", status_code=202, summary="Log engagement event")
async def log_event(body: EngagementEvent):
    """Record a generic engagement event (signup, first trade, share, etc.).

    Fire-and-forget — always returns 202. DB errors are logged server-side
    but never surfaced to the caller.
    """
    try:
        client = get_client()
        client.table("engagement_events").insert({
            "user_address": body.user_address.lower() if body.user_address else None,
            "event_type": body.event_type,
            "metadata": body.metadata,
        }).execute()
    except Exception:
        logger.error("Failed to log engagement event", exc_info=True)

    return {"ok": True}
