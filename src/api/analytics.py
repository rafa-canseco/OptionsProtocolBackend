import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])


class SliderInteraction(BaseModel):
    session_id: str
    selected_price: float
    side: str = "buy"
    shown_premium: float | None = None
    converted_to_signup: bool = False


class EngagementEvent(BaseModel):
    user_address: str | None = None
    event_type: str  # slider_use, signup, first_trade, return_visit, share_result, settle
    metadata: dict = {}


@router.post("/slider", status_code=202)
async def log_slider(body: SliderInteraction):
    """Log a slider interaction. Fire-and-forget — never fails the request."""
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
        logger.warning("Failed to log slider interaction", exc_info=True)

    return {"ok": True}


@router.post("/event", status_code=202)
async def log_event(body: EngagementEvent):
    """Log an engagement event. Fire-and-forget — never fails the request."""
    try:
        client = get_client()
        client.table("engagement_events").insert({
            "user_address": body.user_address.lower() if body.user_address else None,
            "event_type": body.event_type,
            "metadata": body.metadata,
        }).execute()
    except Exception:
        logger.warning("Failed to log engagement event", exc_info=True)

    return {"ok": True}
