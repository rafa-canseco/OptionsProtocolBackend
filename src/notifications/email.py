"""Resend email wrapper for b1nary notifications.

All send functions are no-ops when settings.resend_api_key is empty,
allowing the system to run without email support configured.
"""

import hashlib
import hmac
import logging
import urllib.parse

import resend

from src.config import settings
from src.notifications.templates import (
    render_verification_email as _render_verification,
    render_reminder_email as _render_reminder,
    render_result_email_otm as _render_otm,
    render_result_email_itm as _render_itm,
    render_result_email_consolidated as _render_consolidated,
)

logger = logging.getLogger(__name__)


def _init_resend() -> bool:
    """Set Resend API key. Returns False if not configured."""
    if not settings.resend_api_key:
        return False
    resend.api_key = settings.resend_api_key
    return True


def send_verification_email(email: str, code: str) -> None:
    if not _init_resend():
        logger.warning("Resend not configured, skipping verification email")
        return
    subject, html = _render_verification(code)
    params: resend.Emails.SendParams = {
        "from": settings.email_from,
        "to": [email],
        "subject": subject,
        "html": html,
    }
    result = resend.Emails.send(params)
    logger.info("Verification email sent to %s: %s", email, result.get("id"))


def generate_unsubscribe_url(wallet_address: str) -> str:
    if not settings.unsubscribe_secret:
        raise RuntimeError(
            "UNSUBSCRIBE_SECRET must be set when email notifications are enabled. "
            'Generate with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    token = hmac.new(
        settings.unsubscribe_secret.encode(),
        wallet_address.lower().encode(),
        hashlib.sha256,
    ).hexdigest()
    params = urllib.parse.urlencode(
        {
            "token": token,
            "wallet": wallet_address.lower(),
        }
    )
    base = settings.api_base_url.rstrip("/")
    return f"{base}/notifications/unsubscribe?{params}"


def verify_unsubscribe_token(wallet_address: str, token: str) -> bool:
    if not settings.unsubscribe_secret:
        return False
    expected = hmac.new(
        settings.unsubscribe_secret.encode(),
        wallet_address.lower().encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(token, expected)


def _inject_unsubscribe_url(html: str, wallet_address: str) -> str:
    url = generate_unsubscribe_url(wallet_address)
    return html.replace("{unsubscribe_url}", url)


def send_batch(emails: list[dict]) -> list[dict]:
    """Send a batch of emails via Resend.

    Each dict in emails must have: to, subject, html.
    Returns list of Resend responses (one per email).
    Chunks into groups of 100 (Resend batch limit).
    """
    if not _init_resend():
        logger.warning("Resend not configured, skipping batch send")
        return []
    if not emails:
        return []

    all_results: list[dict] = []
    for i in range(0, len(emails), 100):
        chunk = emails[i : i + 100]
        params_list: list[resend.Emails.SendParams] = [
            {
                "from": settings.email_from,
                "to": [e["to"]],
                "subject": e["subject"],
                "html": e["html"],
                "headers": e.get("headers", {}),
            }
            for e in chunk
        ]
        results = resend.Batch.send(params_list)
        if not isinstance(results, list):
            raise TypeError(
                f"resend.Batch.send returned unexpected type "
                f"{type(results).__name__}: {results!r}"
            )
        all_results.extend(results)

    logger.info("Batch email sent: %d emails", len(all_results))
    return all_results


def build_reminder_email(
    email: str,
    wallet_address: str,
    asset: str,
    strike_usd: str,
    option_type: str,
    expiry_date: str,
) -> dict:
    """Build a reminder email dict ready for send_batch."""
    subject, html = _render_reminder(asset, strike_usd, option_type, expiry_date)
    html = _inject_unsubscribe_url(html, wallet_address)
    unsub_url = generate_unsubscribe_url(wallet_address)
    return {
        "to": email,
        "subject": subject,
        "html": html,
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def build_result_email_otm(
    email: str,
    wallet_address: str,
    collateral_usd: str,
    premium_usd: str,
    asset: str,
) -> dict:
    """Build an OTM result email dict ready for send_batch."""
    subject, html = _render_otm(collateral_usd, premium_usd, asset)
    html = _inject_unsubscribe_url(html, wallet_address)
    unsub_url = generate_unsubscribe_url(wallet_address)
    return {
        "to": email,
        "subject": subject,
        "html": html,
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def build_consolidated_result_email(
    email: str,
    wallet_address: str,
    positions: list[dict],
) -> dict:
    """Build a consolidated settlement result email for all of a wallet's positions.

    Each position dict must satisfy render_result_email_consolidated's requirements.
    """
    subject, html = _render_consolidated(positions)
    html = _inject_unsubscribe_url(html, wallet_address)
    unsub_url = generate_unsubscribe_url(wallet_address)
    return {
        "to": email,
        "subject": subject,
        "html": html,
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def build_result_email_itm(
    email: str,
    wallet_address: str,
    asset: str,
    amount: str,
    strike_usd: str,
    is_put: bool,
) -> dict:
    """Build an ITM result email dict ready for send_batch."""
    subject, html = _render_itm(asset, amount, strike_usd, is_put)
    html = _inject_unsubscribe_url(html, wallet_address)
    unsub_url = generate_unsubscribe_url(wallet_address)
    return {
        "to": email,
        "subject": subject,
        "html": html,
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
