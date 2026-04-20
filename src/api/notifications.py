import logging
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from src.db.database import get_client
from src.models.notification import (
    EmailSubmitRequest,
    EmailVerifyRequest,
    NotificationStatusResponse,
)
from src.notifications.email import (
    send_verification_email,
    verify_unsubscribe_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["Notifications"])

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# --- Rate limiting ---
_MAX_TRACKED = 10_000

_WALLET_WINDOW = 3600  # 1 hour
_WALLET_MAX = 3
_wallet_hits: dict[str, list[float]] = defaultdict(list)

_IP_WINDOW = 3600
_IP_MAX = 10
_ip_hits: dict[str, list[float]] = defaultdict(list)


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _check_wallet_rate_limit(wallet: str) -> None:
    now = time.monotonic()
    if len(_wallet_hits) > _MAX_TRACKED:
        stale = [
            k for k, v in _wallet_hits.items() if not v or now - v[-1] >= _WALLET_WINDOW
        ]
        for k in stale:
            del _wallet_hits[k]

    hits = _wallet_hits[wallet]
    _wallet_hits[wallet] = [t for t in hits if now - t < _WALLET_WINDOW]
    if len(_wallet_hits[wallet]) >= _WALLET_MAX:
        raise HTTPException(429, "Too many verification attempts, try again later")
    _wallet_hits[wallet].append(now)


def _check_ip_rate_limit(ip: str) -> None:
    now = time.monotonic()
    if len(_ip_hits) > _MAX_TRACKED:
        stale = [k for k, v in _ip_hits.items() if not v or now - v[-1] >= _IP_WINDOW]
        for k in stale:
            del _ip_hits[k]

    hits = _ip_hits[ip]
    _ip_hits[ip] = [t for t in hits if now - t < _IP_WINDOW]
    if len(_ip_hits[ip]) >= _IP_MAX:
        raise HTTPException(429, "Too many requests, try again later")
    _ip_hits[ip].append(now)


@router.post("/email", summary="Submit email for verification")
async def submit_email(body: EmailSubmitRequest, request: Request):
    """Submit an email address for notification opt-in.

    Sends a 6-digit verification code via Resend. Rate limited to
    3 per wallet per hour and 10 per IP per hour.
    """
    wallet = body.wallet_address.lower()
    _check_wallet_rate_limit(wallet)
    _check_ip_rate_limit(_get_client_ip(request))

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    client = get_client()
    try:
        client.table("user_emails").upsert(
            {
                "wallet_address": wallet,
                "email": body.email,
                "verification_code": code,
                "code_expires_at": expires_at,
                "verified_at": None,
                "unsubscribed_at": None,
                "updated_at": now,
            },
            on_conflict="wallet_address",
        ).execute()
    except Exception:
        logger.exception("Failed to upsert user_emails")
        raise HTTPException(502, "Could not save email")

    try:
        send_verification_email(body.email, code)
    except Exception:
        logger.exception("Failed to send verification email")
        raise HTTPException(502, "Could not send verification email")

    return {"ok": True}


@router.post("/verify", summary="Verify email code")
async def verify_email(body: EmailVerifyRequest):
    """Verify a 6-digit code sent to the user's email.

    On success, sets verified_at and clears the code.
    """
    wallet = body.wallet_address.lower()
    client = get_client()
    try:
        result = (
            client.table("user_emails")
            .select("verification_code, code_expires_at")
            .eq("wallet_address", wallet)
            .execute()
        )
    except Exception:
        logger.exception("Failed to query user_emails")
        raise HTTPException(502, "Verification failed")

    if not result.data:
        raise HTTPException(404, "No email registered for this wallet")

    row = result.data[0]
    stored_code = row.get("verification_code")
    expires_at_str = row.get("code_expires_at")

    if not stored_code or stored_code != body.code:
        raise HTTPException(400, "Invalid verification code")

    if expires_at_str:
        expires_at = datetime.fromisoformat(expires_at_str)
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(400, "Verification code has expired")

    now = datetime.now(timezone.utc).isoformat()
    try:
        client.table("user_emails").update(
            {
                "verified_at": now,
                "verification_code": None,
                "code_expires_at": None,
                "updated_at": now,
            }
        ).eq("wallet_address", wallet).execute()
    except Exception:
        logger.exception("Failed to update verified_at")
        raise HTTPException(502, "Verification failed")

    return {"ok": True, "verified": True}


@router.get(
    "/status",
    response_model=NotificationStatusResponse,
    summary="Get notification status for a wallet",
)
async def get_status(
    wallet: str = Query(description="Ethereum wallet address"),
):
    """Return notification opt-in status for the given wallet."""
    if not ETH_ADDRESS_RE.match(wallet):
        raise HTTPException(400, "Invalid Ethereum address")

    client = get_client()
    try:
        result = (
            client.table("user_emails")
            .select("email, verified_at, unsubscribed_at")
            .eq("wallet_address", wallet.lower())
            .execute()
        )
    except Exception:
        logger.exception("Failed to query user_emails")
        raise HTTPException(502, "Could not fetch status")

    if not result.data:
        return NotificationStatusResponse(
            has_email=False, verified=False, unsubscribed=False
        )

    row = result.data[0]
    return NotificationStatusResponse(
        has_email=True,
        verified=row.get("verified_at") is not None,
        unsubscribed=row.get("unsubscribed_at") is not None,
    )


@router.get(
    "/unsubscribe",
    response_class=HTMLResponse,
    summary="Unsubscribe via email link",
)
async def unsubscribe(
    wallet: str = Query(description="Wallet address"),
    token: str = Query(description="HMAC unsubscribe token"),
):
    """Unsubscribe from notifications via signed email link.

    Supports both GET (browser click) and POST (RFC 8058 one-click).
    """
    return _process_unsubscribe(wallet, token)


@router.post(
    "/unsubscribe",
    response_class=HTMLResponse,
    summary="Unsubscribe via one-click (RFC 8058)",
    include_in_schema=False,
)
async def unsubscribe_post(
    wallet: str = Query(description="Wallet address"),
    token: str = Query(description="HMAC unsubscribe token"),
):
    """POST handler for RFC 8058 List-Unsubscribe-Post one-click."""
    return _process_unsubscribe(wallet, token)


def _process_unsubscribe(wallet: str, token: str) -> HTMLResponse:
    if not verify_unsubscribe_token(wallet, token):
        raise HTTPException(403, "Invalid unsubscribe token")

    from src.notifications.templates import render_unsubscribe_page

    now = datetime.now(timezone.utc).isoformat()
    client = get_client()
    try:
        client.table("user_emails").update(
            {"unsubscribed_at": now, "updated_at": now}
        ).eq("wallet_address", wallet.lower()).execute()
    except Exception:
        logger.exception("Failed to unsubscribe wallet %s", wallet)
        return HTMLResponse(
            content=(
                "<html><body style='font-family:sans-serif;text-align:center;padding:40px;'>"
                "<p>Unsubscribe failed — please try again later.</p>"
                "</body></html>"
            ),
            status_code=502,
        )

    return HTMLResponse(content=render_unsubscribe_page(), status_code=200)
