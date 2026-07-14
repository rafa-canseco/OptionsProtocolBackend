"""Dynerox fiat on-ramp integration.

The browser must never call Dynerox directly because every public Dynerox
endpoint requires the merchant API key in `x-api-key`.
"""

import hmac
import hashlib
import logging
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from web3 import Web3

from src.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dynerox", tags=["Dynerox"])

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
MAX_WEBHOOK_SKEW_SECONDS = 5 * 60


class DyneroxOnrampRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    middle_name: str | None = Field(default=None, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    second_last_name: str | None = Field(default=None, max_length=80)
    email: EmailStr
    curp: str = Field(min_length=18, max_length=18)
    phone: str | None = Field(default=None, max_length=32)
    wallet_address: str = Field(
        description="Base wallet that will receive USDC from Dynerox",
        examples=["0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"],
    )

    @field_validator("curp")
    @classmethod
    def normalize_curp(cls, value: str) -> str:
        curp = value.strip().upper()
        if not re.match(r"^[A-Z0-9]{18}$", curp):
            raise ValueError("Invalid CURP")
        return curp

    @field_validator("wallet_address")
    @classmethod
    def validate_wallet(cls, value: str) -> str:
        if not ETH_ADDRESS_RE.match(value):
            raise ValueError("Invalid Base wallet address")
        return Web3.to_checksum_address(value)


class DyneroxOnrampResponse(BaseModel):
    user_id: str
    route_id: str
    status: str
    authorization_url: str | None
    deposit_account: str | None = Field(
        default=None,
        description="SPEI CLABE assigned by Dynerox after route authorization, if already active.",
    )


class DyneroxWebhookAck(BaseModel):
    received: bool = True


def _dynerox_headers() -> dict[str, str]:
    if not settings.dynerox_api_key:
        raise HTTPException(503, "Dynerox API key is not configured")
    return {
        "x-api-key": settings.dynerox_api_key,
        "content-type": "application/json",
    }


def _dynerox_url(path: str) -> str:
    return f"{settings.dynerox_api_url.rstrip('/')}{path}"


async def _dynerox_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                _dynerox_url(path),
                headers=_dynerox_headers(),
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.exception("Dynerox request failed: %s", path)
        raise HTTPException(502, f"Dynerox request failed: {type(exc).__name__}")

    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        logger.warning("Dynerox returned %s for %s: %s", response.status_code, path, detail)
        raise HTTPException(response.status_code, detail)

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(502, "Dynerox returned invalid JSON")
    if not isinstance(data, dict):
        raise HTTPException(502, "Dynerox returned an unexpected response")
    return data


@router.post(
    "/onramp",
    response_model=DyneroxOnrampResponse,
    summary="Start MXN to Base USDC onboarding via Dynerox",
)
async def start_dynerox_onramp(body: DyneroxOnrampRequest):
    """Create a Dynerox user and MXN/SPEI -> USDC/Base route.

    Dynerox returns an authorization URL. After the user authorizes, Dynerox
    activates the route and assigns the permanent SPEI deposit CLABE in
    `from.account`.
    """
    user_payload: dict[str, Any] = {
        "first_name": body.first_name,
        "last_name": body.last_name,
        "email": str(body.email),
        "curp": body.curp,
    }
    if body.middle_name:
        user_payload["middle_name"] = body.middle_name
    if body.second_last_name:
        user_payload["second_last_name"] = body.second_last_name
    if body.phone:
        user_payload["phone"] = body.phone

    user = await _dynerox_post("/v1/public/users", user_payload)
    user_id = user.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(502, "Dynerox user response did not include user_id")

    route_payload = {
        "user_id": user_id,
        "from": {
            "currency": "MXN",
            "network": "SPEI",
        },
        "to": {
            "currency": settings.dynerox_onramp_currency,
            "network": settings.dynerox_onramp_network,
            "account": body.wallet_address,
        },
    }
    route = await _dynerox_post("/v1/public/routes", route_payload)

    route_id = route.get("route_id")
    status = route.get("status")
    if not isinstance(route_id, str) or not isinstance(status, str):
        raise HTTPException(502, "Dynerox route response is missing route_id/status")

    from_leg = route.get("from") if isinstance(route.get("from"), dict) else {}
    deposit_account = from_leg.get("account") if isinstance(from_leg, dict) else None

    return DyneroxOnrampResponse(
        user_id=user_id,
        route_id=route_id,
        status=status,
        authorization_url=route.get("authorization_url"),
        deposit_account=deposit_account if isinstance(deposit_account, str) else None,
    )


def _verify_webhook_signature(raw_body: bytes, timestamp: str | None, signature: str | None) -> None:
    if not settings.dynerox_webhook_secret:
        raise HTTPException(503, "Dynerox webhook secret is not configured")
    if not timestamp or not signature:
        raise HTTPException(401, "Missing Dynerox webhook signature")
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(401, "Invalid Dynerox webhook timestamp")
    if abs(int(time.time()) - ts) > MAX_WEBHOOK_SKEW_SECONDS:
        raise HTTPException(401, "Stale Dynerox webhook timestamp")

    payload = timestamp.encode() + b"." + raw_body
    expected = hmac.new(
        settings.dynerox_webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid Dynerox webhook signature")


@router.post(
    "/webhook",
    response_model=DyneroxWebhookAck,
    summary="Receive Dynerox webhook events",
)
async def dynerox_webhook(
    request: Request,
    x_dynerox_signature: str | None = Header(default=None),
    x_webhook_signature: str | None = Header(default=None),
    x_dynerox_timestamp: str | None = Header(default=None),
    x_webhook_timestamp: str | None = Header(default=None),
):
    """Validate and acknowledge Dynerox webhooks.

    Dynerox docs mention both `x-dynerox-signature` and
    `x-webhook-signature` in different sections, so we accept either header.
    """
    raw_body = await request.body()
    _verify_webhook_signature(
        raw_body,
        x_dynerox_timestamp or x_webhook_timestamp,
        x_dynerox_signature or x_webhook_signature,
    )
    logger.info("Received Dynerox webhook: %s", raw_body.decode(errors="ignore")[:500])
    return DyneroxWebhookAck()
