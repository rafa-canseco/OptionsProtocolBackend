# Email Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add email notifications for expiry reminders (24h before) and settlement results (after batchSettleVaults), with a verification flow for email opt-in.

**Architecture:** Hybrid — a dedicated `notification_bot.py` sends reminders every 30 min; settlement result emails fire inline from `expiry_settler.py` after `settle_once()`. Email sending is wrapped in `src/notifications/email.py` using Resend's Python SDK. All emails use branded HTML templates.

**Tech Stack:** Python 3.13, FastAPI, Resend SDK, Supabase (Postgres), Pydantic v2, HMAC-SHA256 for unsubscribe tokens.

**Spec:** `docs/superpowers/specs/2026-03-26-email-notifications-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `supabase/migrations/20260326_user_emails.sql` | Create | DB migration: `user_emails` table + `order_events` columns |
| `src/config.py` | Modify | Add Resend/email settings |
| `src/models/notification.py` | Create | Pydantic models for notification endpoints |
| `src/notifications/__init__.py` | Create | Package init |
| `src/notifications/templates.py` | Create | Branded HTML email templates |
| `src/notifications/email.py` | Create | Resend wrapper: send_verification, send_reminder_batch, send_result_batch |
| `src/api/notifications.py` | Create | API endpoints: submit email, verify, status, unsubscribe |
| `src/bots/notification_bot.py` | Create | Reminder check loop (every 30 min) |
| `src/bots/expiry_settler.py` | Modify | Fire result emails after settlement |
| `src/main.py` | Modify | Include router, start bot, add OpenAPI tag |
| `pyproject.toml` | Modify | Add `resend` dependency |
| `tests/test_notifications_api.py` | Create | API endpoint tests |
| `tests/test_notification_bot.py` | Create | Bot logic tests |
| `tests/test_email_service.py` | Create | Email sending tests |
| `tests/test_settler_emails.py` | Create | Settler integration tests |

---

## Task 1: Project Setup (dependency + config + migration)

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/config.py`
- Create: `supabase/migrations/20260326_user_emails.sql`

- [ ] **Step 1: Add resend dependency**

```bash
cd /Users/rafa/Desktop/SoftwareDevelopment/personal/options/backend
uv add resend
```

- [ ] **Step 2: Add config settings**

In `src/config.py`, add these fields to the `Settings` class after the `eth_staking_apy` line:

```python
    # Email notifications (Resend)
    resend_api_key: str = ""
    email_from: str = "b1nary <notifications@b1nary.app>"
    api_base_url: str = "https://api.b1nary.app"  # for absolute URLs in emails
    unsubscribe_secret: str = ""
    notification_check_interval_seconds: int = 1800  # 30 min
```

- [ ] **Step 3: Create database migration**

Create `supabase/migrations/20260326_user_emails.sql`:

```sql
-- Email notification opt-in table
CREATE TABLE IF NOT EXISTS user_emails (
    wallet_address TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    verified_at TIMESTAMPTZ,
    verification_code TEXT,
    code_expires_at TIMESTAMPTZ,
    unsubscribed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Fast lookup for verified, subscribed users (used by reminder/result queries)
CREATE INDEX IF NOT EXISTS idx_user_emails_verified
    ON user_emails (wallet_address)
    WHERE verified_at IS NOT NULL AND unsubscribed_at IS NULL;

-- Dedup columns on order_events: max 1 reminder + 1 result email per position
ALTER TABLE order_events ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ;
ALTER TABLE order_events ADD COLUMN IF NOT EXISTS result_sent_at TIMESTAMPTZ;
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock src/config.py supabase/migrations/20260326_user_emails.sql
git commit -m "feat(B1N-232): add resend dependency, config, and migration"
```

---

## Task 2: Pydantic Models

**Files:**
- Create: `src/models/notification.py`

- [ ] **Step 1: Create notification models**

Create `src/models/notification.py`:

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add src/models/notification.py
git commit -m "feat(B1N-232): add notification pydantic models"
```

---

## Task 3: Email Templates

**Files:**
- Create: `src/notifications/__init__.py`
- Create: `src/notifications/templates.py`
- Create: `tests/test_email_service.py` (template rendering tests)

- [ ] **Step 1: Create package init**

Create empty `src/notifications/__init__.py`.

- [ ] **Step 2: Write template rendering tests**

Create `tests/test_email_service.py`:

```python
from src.notifications.templates import (
    render_verification_email,
    render_reminder_email,
    render_result_email_otm,
    render_result_email_itm,
    render_unsubscribe_page,
)


def test_verification_email_contains_code():
    subject, html = render_verification_email("384921")
    assert "384921" in html
    assert "10 minutes" in html
    assert "verification" in subject.lower()


def test_reminder_email_contains_position_details():
    subject, html = render_reminder_email(
        asset="ETH",
        strike_usd="2,075",
        option_type="put",
        expiry_date="2026-03-27",
    )
    assert "ETH" in subject
    assert "$2,075" in subject
    assert "put" in subject
    assert "8:00 AM UTC" in html
    # Template returns raw {unsubscribe_url} placeholder — caller replaces it
    assert "{unsubscribe_url}" in html


def test_result_email_otm():
    subject, html = render_result_email_otm(
        collateral_usd="1,000",
        premium_usd="15.00",
        asset="ETH",
    )
    assert "$1,000" in html
    assert "$15.00" in html
    assert "back" in html.lower()


def test_result_email_itm_put():
    subject, html = render_result_email_itm(
        asset="ETH",
        amount="0.4800",
        strike_usd="2,075",
        is_put=True,
    )
    assert "bought" in subject.lower() or "bought" in html.lower()
    assert "0.4800" in html
    assert "ETH" in html


def test_result_email_itm_call():
    subject, html = render_result_email_itm(
        asset="ETH",
        amount="0.4800",
        strike_usd="2,800",
        is_put=False,
    )
    assert "sold" in subject.lower() or "sold" in html.lower()


def test_unsubscribe_page():
    html = render_unsubscribe_page()
    assert "unsubscribed" in html.lower()
    assert "<html" in html.lower()
```

- [ ] **Step 3: Run tests — verify they fail**

```bash
uv run pytest tests/test_email_service.py -v
```

Expected: FAIL (module not found)

- [ ] **Step 4: Implement templates**

Create `src/notifications/templates.py`. This module returns `(subject, html_body)` tuples. The HTML uses inline CSS for email client compatibility. The brand color is `#6366f1` (indigo). All templates include an `{unsubscribe_url}` placeholder that the caller replaces.

```python
"""Branded HTML email templates for b1nary notifications.

Each render_* function returns (subject: str, html: str).
Templates use {unsubscribe_url} placeholder — callers must .format() it.
"""

_BRAND_COLOR = "#6366f1"
_BG_COLOR = "#0f0f14"
_TEXT_COLOR = "#e2e2e9"
_MUTED_COLOR = "#9ca3af"

_BASE_STYLE = f"""
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:{_BG_COLOR};font-family:
-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{_BG_COLOR};">
<tr><td align="center" style="padding:40px 20px;">
<table width="560" cellpadding="0" cellspacing="0" style="background:#1a1a24;
border-radius:12px;padding:40px;">
<tr><td>
<div style="text-align:center;margin-bottom:32px;">
<span style="font-size:24px;font-weight:700;color:white;">b1nary</span>
</div>
{{content}}
</td></tr>
</table>
<table width="560" cellpadding="0" cellspacing="0">
<tr><td style="padding:20px 0;text-align:center;color:{_MUTED_COLOR};font-size:12px;">
{{footer}}
</td></tr>
</table>
</td></tr>
</table>
</body></html>
"""


def _wrap(content: str, footer: str = "") -> str:
    return _BASE_STYLE.replace("{{content}}", content).replace(
        "{{footer}}", footer
    )


def _unsub_footer() -> str:
    return (
        '<a href="{unsubscribe_url}" style="color:#9ca3af;text-decoration:underline;">'
        "Unsubscribe</a> from b1nary notifications"
    )


def render_verification_email(code: str) -> tuple[str, str]:
    subject = "Your b1nary verification code"
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 16px;">
    Your verification code is:</p>
    <div style="text-align:center;margin:24px 0;">
    <span style="font-size:36px;font-weight:700;letter-spacing:8px;color:white;
    background:#2a2a3a;padding:16px 32px;border-radius:8px;display:inline-block;">
    {code}</span></div>
    <p style="color:{_MUTED_COLOR};font-size:14px;margin:0;">
    This code expires in 10 minutes.</p>
    """
    footer = f'<span style="color:{_MUTED_COLOR};font-size:12px;">b1nary options</span>'
    return subject, _wrap(content, footer)


def render_reminder_email(
    asset: str,
    strike_usd: str,
    option_type: str,
    expiry_date: str,
) -> tuple[str, str]:
    subject = f"Your {asset} ${strike_usd} {option_type} expires tomorrow at 8:00 AM UTC"
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 16px;">
    Your <strong style="color:white;">{asset} ${strike_usd} {option_type}</strong>
    expires tomorrow at <strong style="color:white;">8:00 AM UTC</strong>
    ({expiry_date}).</p>
    <p style="color:{_MUTED_COLOR};font-size:14px;margin:0 0 24px;">
    No action needed — settlement is automatic. We'll email your result.</p>
    <div style="text-align:center;">
    <a href="https://app.b1nary.app" style="display:inline-block;background:{_BRAND_COLOR};
    color:white;padding:12px 32px;border-radius:8px;text-decoration:none;
    font-weight:600;font-size:14px;">View position</a></div>
    """
    return subject, _wrap(content, _unsub_footer())


def render_result_email_otm(
    collateral_usd: str,
    premium_usd: str,
    asset: str,
) -> tuple[str, str]:
    subject = f"Your {asset} option expired OTM — collateral returned"
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 8px;">
    Your <strong style="color:white;">${collateral_usd}</strong> is back
    + you kept <strong style="color:#34d399;">${premium_usd}</strong> premium.</p>
    <div style="text-align:center;margin-top:24px;">
    <a href="https://app.b1nary.app" style="display:inline-block;background:{_BRAND_COLOR};
    color:white;padding:12px 32px;border-radius:8px;text-decoration:none;
    font-weight:600;font-size:14px;">Earn again</a></div>
    """
    return subject, _wrap(content, _unsub_footer())


def render_result_email_itm(
    asset: str,
    amount: str,
    strike_usd: str,
    is_put: bool,
) -> tuple[str, str]:
    if is_put:
        verb = "Bought"
        cta = "Sell higher"
    else:
        verb = "Sold"
        cta = "View position"
    subject = f"You {verb.lower()} {amount} {asset} at ${strike_usd}"
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 8px;">
    You {verb.lower()} <strong style="color:white;">{amount} {asset}</strong>
    at <strong style="color:white;">${strike_usd}</strong>.</p>
    <div style="text-align:center;margin-top:24px;">
    <a href="https://app.b1nary.app" style="display:inline-block;background:{_BRAND_COLOR};
    color:white;padding:12px 32px;border-radius:8px;text-decoration:none;
    font-weight:600;font-size:14px;">{cta}</a></div>
    """
    return subject, _wrap(content, _unsub_footer())


def render_unsubscribe_page() -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Unsubscribed</title></head>
<body style="margin:0;padding:0;background:{_BG_COLOR};font-family:
-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
display:flex;justify-content:center;align-items:center;min-height:100vh;">
<div style="text-align:center;color:{_TEXT_COLOR};">
<p style="font-size:24px;font-weight:700;color:white;">b1nary</p>
<p style="font-size:16px;">You've been unsubscribed from b1nary notifications.</p>
<p style="font-size:14px;color:{_MUTED_COLOR};">You can re-subscribe anytime from the app.</p>
</div></body></html>"""
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
uv run pytest tests/test_email_service.py -v
```

Expected: all 6 PASS

- [ ] **Step 6: Commit**

```bash
git add src/notifications/__init__.py src/notifications/templates.py tests/test_email_service.py
git commit -m "feat(B1N-232): add branded email templates with tests"
```

---

## Task 4: Email Service (Resend wrapper)

**Files:**
- Create: `src/notifications/email.py`
- Modify: `tests/test_email_service.py`

- [ ] **Step 1: Write email service tests**

Append to `tests/test_email_service.py`:

```python
from unittest.mock import patch, MagicMock
from src.notifications.email import (
    send_verification_email,
    send_batch,
    generate_unsubscribe_url,
    verify_unsubscribe_token,
)


def test_send_verification_email_calls_resend():
    with patch("src.notifications.email.resend") as mock_resend:
        mock_resend.Emails.send.return_value = {"id": "test-id"}
        send_verification_email("user@example.com", "384921")
        mock_resend.Emails.send.assert_called_once()
        params = mock_resend.Emails.send.call_args[0][0]
        assert params["to"] == ["user@example.com"]
        assert "384921" in params["html"]


def test_send_verification_email_noop_when_no_api_key():
    with (
        patch("src.notifications.email.resend") as mock_resend,
        patch("src.notifications.email.settings") as mock_settings,
    ):
        mock_settings.resend_api_key = ""
        send_verification_email("user@example.com", "384921")
        mock_resend.Emails.send.assert_not_called()


def test_send_batch_calls_resend_batch():
    emails = [
        {"to": "a@b.com", "subject": "test", "html": "<p>hi</p>"},
        {"to": "c@d.com", "subject": "test2", "html": "<p>hi2</p>"},
    ]
    with patch("src.notifications.email.resend") as mock_resend:
        mock_resend.Batch.send.return_value = [
            {"id": "id1"},
            {"id": "id2"},
        ]
        results = send_batch(emails)
        mock_resend.Batch.send.assert_called_once()
        assert len(results) == 2


def test_unsubscribe_token_roundtrip():
    wallet = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
    with patch("src.notifications.email.settings") as mock_settings:
        mock_settings.unsubscribe_secret = "test-secret-key"
        mock_settings.api_base_url = "https://api.b1nary.app"
        url = generate_unsubscribe_url(wallet)
        assert url.startswith("https://api.b1nary.app/")
        assert "token=" in url
        assert "wallet=" in url
        # Extract token from URL
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        token = params["token"][0]
        assert verify_unsubscribe_token(wallet, token) is True
        assert verify_unsubscribe_token(wallet, "bad-token") is False
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_email_service.py::test_send_verification_email_calls_resend -v
```

Expected: FAIL (import error)

- [ ] **Step 3: Implement email service**

Create `src/notifications/email.py`:

```python
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
    token = hmac.new(
        settings.unsubscribe_secret.encode(),
        wallet_address.lower().encode(),
        hashlib.sha256,
    ).hexdigest()
    params = urllib.parse.urlencode({
        "token": token,
        "wallet": wallet_address.lower(),
    })
    base = settings.api_base_url.rstrip("/")
    return f"{base}/notifications/unsubscribe?{params}"


def verify_unsubscribe_token(wallet_address: str, token: str) -> bool:
    expected = hmac.new(
        settings.unsubscribe_secret.encode(),
        wallet_address.lower().encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(token, expected)


def _inject_unsubscribe_url(html: str, wallet_address: str) -> str:
    url = generate_unsubscribe_url(wallet_address)
    return html.replace("{unsubscribe_url}", url)


def send_batch(
    emails: list[dict],
) -> list[dict]:
    """Send a batch of emails via Resend.

    Each dict in emails must have: to, subject, html.
    Returns list of Resend responses (one per email).
    """
    if not _init_resend():
        logger.warning("Resend not configured, skipping batch send")
        return []
    if not emails:
        return []

    # Resend batch limit is 100 emails per call — chunk accordingly
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


def build_result_email_itm(
    email: str,
    wallet_address: str,
    asset: str,
    amount: str,
    strike_usd: str,
    is_put: bool,
) -> dict:
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
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_email_service.py -v
```

Expected: all 10 PASS

- [ ] **Step 5: Commit**

```bash
git add src/notifications/email.py tests/test_email_service.py
git commit -m "feat(B1N-232): add Resend email service with tests"
```

---

## Task 5: Notification API Endpoints

**Files:**
- Create: `src/api/notifications.py`
- Create: `tests/test_notifications_api.py`

- [ ] **Step 1: Write API tests**

Create `tests/test_notifications_api.py`:

```python
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

VALID_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
VALID_EMAIL = "user@example.com"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear notification rate-limit state between tests."""
    from src.api import notifications as mod
    mod._wallet_hits.clear()
    mod._ip_hits.clear()
    yield
    mod._wallet_hits.clear()
    mod._ip_hits.clear()


def _mock_client_with_data(data):
    """Return a mock Supabase client returning data for any query chain."""
    mock = MagicMock()
    mock_result = MagicMock()
    mock_result.data = data
    # Support arbitrary chaining
    chain = mock.table.return_value
    for method in [
        "select", "eq", "is_", "upsert", "update", "insert",
    ]:
        sub = getattr(chain, method).return_value
        sub.execute.return_value = mock_result
        sub.eq.return_value = sub
        sub.is_.return_value = sub
    return mock


# --- POST /notifications/email ---

def test_submit_email_success():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email") as mock_send,
    ):
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_send.assert_called_once()


def test_submit_email_invalid_wallet():
    resp = client.post(
        "/notifications/email",
        json={"wallet_address": "bad", "email": VALID_EMAIL},
    )
    assert resp.status_code == 422


def test_submit_email_invalid_email():
    resp = client.post(
        "/notifications/email",
        json={"wallet_address": VALID_WALLET, "email": "not-an-email"},
    )
    assert resp.status_code == 422


def test_submit_email_rate_limit_by_wallet():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email"),
    ):
        for _ in range(3):
            resp = client.post(
                "/notifications/email",
                json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
            )
            assert resp.status_code == 200
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
        )
    assert resp.status_code == 429


def test_submit_email_clears_verified_on_change():
    """Changing email resets verified_at (re-verification required)."""
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email"),
    ):
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": "new@example.com"},
        )
    assert resp.status_code == 200
    # Verify upsert was called with verified_at=None
    upsert_call = mock_db.table.return_value.upsert
    upsert_call.assert_called_once()
    upsert_data = upsert_call.call_args[0][0]
    assert upsert_data["verified_at"] is None


def test_submit_email_clears_unsubscribed():
    """Re-registering clears unsubscribed_at."""
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch("src.api.notifications.send_verification_email"),
    ):
        resp = client.post(
            "/notifications/email",
            json={"wallet_address": VALID_WALLET, "email": VALID_EMAIL},
        )
    assert resp.status_code == 200
    upsert_data = mock_db.table.return_value.upsert.call_args[0][0]
    assert upsert_data["unsubscribed_at"] is None


# --- POST /notifications/verify ---

def test_verify_success():
    now = datetime.now(timezone.utc)
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "verification_code": "123456",
        "code_expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.post(
            "/notifications/verify",
            json={"wallet_address": VALID_WALLET, "code": "123456"},
        )
    assert resp.status_code == 200
    assert resp.json()["verified"] is True


def test_verify_wrong_code():
    now = datetime.now(timezone.utc)
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "verification_code": "123456",
        "code_expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.post(
            "/notifications/verify",
            json={"wallet_address": VALID_WALLET, "code": "000000"},
        )
    assert resp.status_code == 400


def test_verify_expired_code():
    past = datetime.now(timezone.utc) - timedelta(minutes=15)
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "verification_code": "123456",
        "code_expires_at": past.isoformat(),
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.post(
            "/notifications/verify",
            json={"wallet_address": VALID_WALLET, "code": "123456"},
        )
    assert resp.status_code == 400


# --- GET /notifications/status ---

def test_status_verified_user():
    row = {
        "wallet_address": VALID_WALLET.lower(),
        "email": VALID_EMAIL,
        "verified_at": "2026-03-26T12:00:00Z",
        "unsubscribed_at": None,
    }
    mock_db = _mock_client_with_data([row])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.get(f"/notifications/status?wallet={VALID_WALLET}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_email"] is True
    assert body["verified"] is True
    assert body["unsubscribed"] is False


def test_status_no_email():
    mock_db = _mock_client_with_data([])
    with patch("src.api.notifications.get_client", return_value=mock_db):
        resp = client.get(f"/notifications/status?wallet={VALID_WALLET}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_email"] is False


# --- GET /notifications/unsubscribe ---

def test_unsubscribe_with_valid_token():
    mock_db = _mock_client_with_data([{"wallet_address": VALID_WALLET.lower()}])
    with (
        patch("src.api.notifications.get_client", return_value=mock_db),
        patch(
            "src.api.notifications.verify_unsubscribe_token", return_value=True
        ),
    ):
        resp = client.get(
            f"/notifications/unsubscribe?wallet={VALID_WALLET}&token=valid"
        )
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()


def test_unsubscribe_with_invalid_token():
    with patch(
        "src.api.notifications.verify_unsubscribe_token", return_value=False
    ):
        resp = client.get(
            f"/notifications/unsubscribe?wallet={VALID_WALLET}&token=bad"
        )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_notifications_api.py -v
```

Expected: FAIL (import error — module doesn't exist)

- [ ] **Step 3: Implement notification endpoints**

Create `src/api/notifications.py`:

```python
import logging
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from src.config import settings
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
            k for k, v in _wallet_hits.items()
            if not v or now - v[-1] >= _WALLET_WINDOW
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
        stale = [
            k for k, v in _ip_hits.items()
            if not v or now - v[-1] >= _IP_WINDOW
        ]
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
    _check_wallet_rate_limit(body.wallet_address)
    _check_ip_rate_limit(_get_client_ip(request))

    code = f"{random.randint(0, 999999):06d}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=10)
    ).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    client = get_client()
    try:
        client.table("user_emails").upsert(
            {
                "wallet_address": body.wallet_address,
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
    client = get_client()
    try:
        result = (
            client.table("user_emails")
            .select("verification_code, code_expires_at")
            .eq("wallet_address", body.wallet_address)
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
        ).eq("wallet_address", body.wallet_address).execute()
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

    return HTMLResponse(content=render_unsubscribe_page(), status_code=200)
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_notifications_api.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/api/notifications.py src/models/notification.py tests/test_notifications_api.py
git commit -m "feat(B1N-232): add notification API endpoints with tests"
```

---

## Task 6: Notification Bot (Reminder Emails)

**Files:**
- Create: `src/bots/notification_bot.py`
- Create: `tests/test_notification_bot.py`

- [ ] **Step 1: Write bot tests**

Create `tests/test_notification_bot.py`:

```python
import time
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta

import pytest
from src.bots.notification_bot import check_once


def _make_position(**overrides) -> dict:
    now_ts = int(time.time())
    defaults = {
        "user_address": "0xabc123",
        "vault_id": 1,
        "otoken_address": "0xdef456",
        "expiry": now_ts + 24 * 3600,  # 24h from now
        "amount": "100000000",  # 1.0 in 8-dec
        "strike_price": "200000000000",  # $2000 in 8-dec
        "is_put": True,
        "asset": "eth",
        "reminder_sent_at": None,
        "is_settled": False,
        "created_at": (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).isoformat(),
    }
    defaults.update(overrides)
    return defaults


def _make_user_email(wallet: str, **overrides) -> dict:
    defaults = {
        "wallet_address": wallet,
        "email": f"{wallet[:8]}@test.com",
        "verified_at": "2026-03-25T12:00:00Z",
        "unsubscribed_at": None,
    }
    defaults.update(overrides)
    return defaults


def _mock_db(positions: list[dict], user_emails: list[dict]):
    """Mock Supabase client with separate responses per table."""
    mock = MagicMock()

    def table_router(name):
        t = MagicMock()
        if name == "order_events":
            result = MagicMock()
            result.data = positions
            # Support the full query chain
            chain = t.select.return_value
            for _ in range(10):
                chain = chain.is_.return_value
                chain.lte.return_value = chain
                chain.gte.return_value = chain
                chain.lt.return_value = chain
                chain.gt.return_value = chain
                chain.eq.return_value = chain
                chain.or_.return_value = chain
            chain.execute.return_value = result
            # For update chain
            update_result = MagicMock()
            update_result.data = [{}]
            uc = t.update.return_value
            uc.eq.return_value = uc
            uc.execute.return_value = update_result
        elif name == "user_emails":
            result = MagicMock()
            result.data = user_emails
            chain = t.select.return_value
            chain.in_.return_value = chain
            chain.is_.return_value = chain
            chain.not_.return_value = chain
            chain.execute.return_value = result
        return t

    mock.table.side_effect = table_router
    return mock


def test_check_once_sends_reminder():
    pos = _make_position(user_address="0xabc123")
    email_row = _make_user_email("0xabc123")
    mock_db = _mock_db([pos], [email_row])

    with (
        patch("src.bots.notification_bot.get_client", return_value=mock_db),
        patch("src.bots.notification_bot.send_batch") as mock_send,
        patch("src.bots.notification_bot.build_reminder_email") as mock_build,
    ):
        mock_build.return_value = {
            "to": "0xabc123@test.com",
            "subject": "test",
            "html": "<p>test</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        check_once()
        mock_build.assert_called_once()
        mock_send.assert_called_once()


def test_check_once_skips_unsubscribed():
    pos = _make_position(user_address="0xabc123")
    email_row = _make_user_email(
        "0xabc123", unsubscribed_at="2026-03-26T12:00:00Z"
    )
    mock_db = _mock_db([pos], [])  # empty because query filters unsubscribed

    with (
        patch("src.bots.notification_bot.get_client", return_value=mock_db),
        patch("src.bots.notification_bot.send_batch") as mock_send,
    ):
        check_once()
        mock_send.assert_not_called()


def test_check_once_skips_already_sent():
    pos = _make_position(
        reminder_sent_at="2026-03-26T12:00:00Z",
    )
    # reminder_sent_at filter is in the DB query, so no positions returned
    mock_db = _mock_db([], [])

    with (
        patch("src.bots.notification_bot.get_client", return_value=mock_db),
        patch("src.bots.notification_bot.send_batch") as mock_send,
    ):
        check_once()
        mock_send.assert_not_called()
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_notification_bot.py -v
```

Expected: FAIL (import error)

- [ ] **Step 3: Implement notification bot**

Create `src/bots/notification_bot.py`:

```python
"""Notification bot — sends expiry reminder emails every 30 minutes.

Queries positions expiring in 20-28h, cross-references with verified
user_emails, and sends reminders via Resend batch API.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from src.config import settings
from src.db.database import get_client
from src.notifications.email import build_reminder_email, send_batch

logger = logging.getLogger(__name__)

_MIN_INTERVAL = 60  # minimum loop interval (safety)


def _get_positions_needing_reminder() -> list[dict]:
    """Query positions expiring in 20-28h that haven't been reminded."""
    now_ts = int(time.time())
    min_expiry = now_ts + 20 * 3600  # 20h from now
    max_expiry = now_ts + 28 * 3600  # 28h from now
    created_before = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()

    client = get_client()
    result = (
        client.table("order_events")
        .select(
            "user_address, vault_id, expiry, amount, strike_price, "
            "is_put, asset, created_at"
        )
        .is_("reminder_sent_at", "null")
        .or_("is_settled.eq.false,is_settled.is.null")
        .gte("expiry", min_expiry)
        .lte("expiry", max_expiry)
        .lt("created_at", created_before)
        .execute()
    )
    return result.data or []


def _get_verified_emails(wallet_addresses: list[str]) -> dict[str, str]:
    """Fetch verified, non-unsubscribed emails for the given wallets.

    Returns {wallet_address: email}.
    """
    if not wallet_addresses:
        return {}
    client = get_client()
    result = (
        client.table("user_emails")
        .select("wallet_address, email")
        .in_("wallet_address", wallet_addresses)
        .not_.is_("verified_at", "null")
        .is_("unsubscribed_at", "null")
        .execute()
    )
    return {
        row["wallet_address"]: row["email"]
        for row in (result.data or [])
    }


def _mark_reminder_sent(user_address: str, vault_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    client = get_client()
    client.table("order_events").update(
        {"reminder_sent_at": now}
    ).eq("user_address", user_address).eq("vault_id", vault_id).execute()


def _format_strike(strike_raw: str | int) -> str:
    return f"{int(strike_raw) / 1e8:,.0f}"


def check_once() -> None:
    """Single reminder check cycle."""
    positions = _get_positions_needing_reminder()
    if not positions:
        logger.debug("No positions needing reminder")
        return

    wallets = list({p["user_address"] for p in positions})
    email_map = _get_verified_emails(wallets)
    if not email_map:
        logger.debug("No verified emails for expiring positions")
        return

    emails_to_send: list[dict] = []
    position_refs: list[tuple[str, int]] = []

    for pos in positions:
        wallet = pos["user_address"]
        email = email_map.get(wallet)
        if not email:
            continue

        asset = (pos.get("asset") or "eth").upper()
        strike_usd = _format_strike(pos["strike_price"])
        option_type = "put" if pos.get("is_put") else "call"
        expiry_ts = pos.get("expiry", 0)
        expiry_date = (
            datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            if expiry_ts
            else "unknown"
        )

        try:
            email_dict = build_reminder_email(
                email=email,
                wallet_address=wallet,
                asset=asset,
                strike_usd=strike_usd,
                option_type=option_type,
                expiry_date=expiry_date,
            )
            emails_to_send.append(email_dict)
            position_refs.append((wallet, pos["vault_id"]))
        except Exception:
            logger.exception(
                "Failed to build reminder email for %s vault %d",
                wallet,
                pos["vault_id"],
            )

    if not emails_to_send:
        return

    logger.info("Sending %d reminder emails", len(emails_to_send))
    try:
        results = send_batch(emails_to_send)
    except Exception:
        logger.exception("Reminder batch send failed")
        return

    # Mark sent for successful emails
    for i, (wallet, vault_id) in enumerate(position_refs):
        if i < len(results) and results[i].get("id"):
            try:
                _mark_reminder_sent(wallet, vault_id)
            except Exception:
                logger.exception(
                    "Failed to mark reminder_sent_at for %s vault %d",
                    wallet,
                    vault_id,
                )
        else:
            logger.warning(
                "Reminder email failed for %s vault %d, will retry",
                wallet,
                vault_id,
            )


async def run():
    """Main loop: check for positions needing reminders."""
    interval = max(settings.notification_check_interval_seconds, _MIN_INTERVAL)
    logger.info("Notification bot starting (interval=%ds)", interval)

    while True:
        try:
            await asyncio.to_thread(check_once)
        except Exception:
            logger.exception("Notification check failed")
        await asyncio.sleep(interval)
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_notification_bot.py -v
```

Expected: all 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/bots/notification_bot.py tests/test_notification_bot.py
git commit -m "feat(B1N-232): add notification bot for reminder emails"
```

---

## Task 7: Settler Integration (Result Emails)

**Files:**
- Modify: `src/bots/expiry_settler.py`
- Create: `tests/test_settler_emails.py`

- [ ] **Step 1: Write settler email tests**

Create `tests/test_settler_emails.py`:

```python
from unittest.mock import MagicMock, patch
import pytest
from src.bots.expiry_settler import _send_settlement_emails


def _make_position(wallet, vault_id, is_itm=False, asset="eth", **kw):
    pos = {
        "user_address": wallet,
        "vault_id": vault_id,
        "otoken_address": "0xtoken",
        "expiry": 1711526400,
        "amount": "100000000",
        "strike_price": "200000000000",
        "is_put": True,
        "asset": asset,
        "is_settled": True,
        "result_sent_at": None,
    }
    if is_itm:
        pos["is_itm"] = True
        pos["settlement_type"] = "physical"
        pos["net_premium"] = "1500000"
    else:
        pos["is_itm"] = False
        pos["net_premium"] = "1500000"
    pos.update(kw)
    return pos


def _mock_db_with_emails(email_map: dict[str, str]):
    mock = MagicMock()

    def table_router(name):
        t = MagicMock()
        if name == "user_emails":
            result = MagicMock()
            result.data = [
                {"wallet_address": w, "email": e}
                for w, e in email_map.items()
            ]
            chain = t.select.return_value
            chain.in_.return_value = chain
            chain.not_.return_value = chain
            chain.is_.return_value = chain
            chain.execute.return_value = result
        elif name == "order_events":
            update_result = MagicMock()
            update_result.data = [{}]
            uc = t.update.return_value
            uc.eq.return_value = uc
            uc.execute.return_value = update_result
        return t

    mock.table.side_effect = table_router
    return mock


def test_sends_otm_result_email():
    pos = _make_position("0xuser1", 1, is_itm=False)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_result_email_otm") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "OTM",
            "html": "<p>otm</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        _send_settlement_emails([pos], [])
        mock_build.assert_called_once()
        mock_send.assert_called_once()


def test_sends_itm_result_email():
    pos = _make_position("0xuser1", 1, is_itm=True)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_result_email_itm") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "ITM",
            "html": "<p>itm</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        _send_settlement_emails([pos], [pos])
        mock_build.assert_called_once()
        mock_send.assert_called_once()


def test_skips_wallet_without_email():
    pos = _make_position("0xnomail", 1, is_itm=False)
    mock_db = _mock_db_with_emails({})  # no emails

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
    ):
        _send_settlement_emails([pos], [])
        mock_send.assert_not_called()


def test_email_failure_does_not_raise():
    pos = _make_position("0xuser1", 1, is_itm=False)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch", side_effect=Exception("boom")),
        patch("src.bots.expiry_settler.build_result_email_otm") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "OTM",
            "html": "<p>otm</p>",
        }
        # Should not raise — fire-and-forget
        _send_settlement_emails([pos], [])
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_settler_emails.py -v
```

Expected: FAIL (function not found)

- [ ] **Step 3: Add settlement email logic to expiry_settler.py**

Add these imports at the top of `src/bots/expiry_settler.py` (after existing imports):

```python
from src.notifications.email import (
    build_result_email_otm,
    build_result_email_itm,
    send_batch,
)
```

Add this function before `settle_once()`:

```python
def _send_settlement_emails(
    settled_positions: list[dict],
    itm_positions: list[dict],
) -> None:
    """Send settlement result emails (fire-and-forget).

    Queries user_emails for verified/subscribed wallets, builds
    OTM or ITM result emails, sends via Resend batch.
    """
    if not settings.resend_api_key:
        return

    all_positions = settled_positions
    wallets = list({p["user_address"] for p in all_positions})
    if not wallets:
        return

    # Fetch verified, subscribed emails
    client = get_client()
    try:
        result = (
            client.table("user_emails")
            .select("wallet_address, email")
            .in_("wallet_address", wallets)
            .not_.is_("verified_at", "null")
            .is_("unsubscribed_at", "null")
            .execute()
        )
        email_map = {
            row["wallet_address"]: row["email"]
            for row in (result.data or [])
        }
    except Exception:
        logger.exception("Failed to fetch user emails for settlement results")
        return

    if not email_map:
        return

    itm_keys = {(p["user_address"], p["vault_id"]) for p in itm_positions}

    emails_to_send: list[dict] = []
    position_refs: list[tuple[str, int]] = []

    for pos in all_positions:
        wallet = pos["user_address"]
        email = email_map.get(wallet)
        if not email:
            continue
        if pos.get("result_sent_at"):
            continue

        vault_id = pos["vault_id"]
        asset = (pos.get("asset") or "eth").upper()
        strike_raw = int(pos.get("strike_price", 0))
        strike_usd = f"{strike_raw / 1e8:,.0f}"
        amount_raw = int(pos.get("amount", 0))
        amount_human = f"{amount_raw / 1e8:.4f}"
        premium_raw = int(pos.get("net_premium") or pos.get("premium") or 0)
        premium_usd = f"{premium_raw / 1e6:.2f}"
        collateral_raw = amount_raw * strike_raw // 10**8
        collateral_usd = f"{collateral_raw / 1e6:,.0f}"

        try:
            if (wallet, vault_id) in itm_keys:
                email_dict = build_result_email_itm(
                    email=email,
                    wallet_address=wallet,
                    asset=asset,
                    amount=amount_human,
                    strike_usd=strike_usd,
                    is_put=pos.get("is_put", True),
                )
            else:
                email_dict = build_result_email_otm(
                    email=email,
                    wallet_address=wallet,
                    collateral_usd=collateral_usd,
                    premium_usd=premium_usd,
                    asset=asset,
                )
            emails_to_send.append(email_dict)
            position_refs.append((wallet, vault_id))
        except Exception:
            logger.exception(
                "Failed to build result email for %s vault %d",
                wallet,
                vault_id,
            )

    if not emails_to_send:
        return

    logger.info("Sending %d settlement result emails", len(emails_to_send))
    try:
        results = send_batch(emails_to_send)
    except Exception:
        logger.exception("Settlement result batch send failed")
        return

    for i, (wallet, vault_id) in enumerate(position_refs):
        if i < len(results) and results[i].get("id"):
            try:
                now = datetime.now(timezone.utc).isoformat()
                _db_update(
                    wallet,
                    vault_id,
                    {"result_sent_at": now},
                    "Settlement email mark",
                )
            except Exception:
                logger.exception(
                    "Failed to mark result_sent_at for %s vault %d",
                    wallet,
                    vault_id,
                )
```

At the end of `settle_once()`, after the OTM block (after the `if skipped_keys:` logging), add:

```python
    # --- Email notifications (fire-and-forget) ---
    try:
        await asyncio.to_thread(
            _send_settlement_emails, settled_positions, itm_positions
        )
    except Exception:
        logger.exception("Settlement emails failed (non-blocking)")
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_settler_emails.py -v
```

Expected: all 4 PASS

- [ ] **Step 5: Run existing settler tests — verify nothing broke**

```bash
uv run pytest tests/test_expiry_settler.py -v
```

Expected: all existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/bots/expiry_settler.py tests/test_settler_emails.py
git commit -m "feat(B1N-232): add settlement result emails to expiry settler"
```

---

## Task 8: Wire Into main.py

**Files:**
- Modify: `src/main.py`

- [ ] **Step 1: Add notification router and bot to main.py**

In `src/main.py`, add the import after the existing router imports:

```python
from src.api.notifications import router as notifications_router
```

Add a `Notifications` tag to `openapi_tags` list:

```python
    {
        "name": "Notifications",
        "description": "Email notification opt-in, verification, and unsubscribe.",
    },
```

Add `app.include_router(notifications_router)` after the existing `include_router` calls.

In the `lifespan` function, add the notification bot startup after the existing bot block. It should have its own guard (only starts when `resend_api_key` is configured):

```python
    if settings.resend_api_key:
        from src.bots import notification_bot

        tasks.append(asyncio.create_task(notification_bot.run()))
        logger.info("Notification bot started")
    else:
        logger.info("Notification bot not started: RESEND_API_KEY not configured")
```

- [ ] **Step 2: Verify the app starts**

```bash
uv run python -c "from src.main import app; print('App created:', app.title)"
```

Expected: `App created: b1nary API`

- [ ] **Step 3: Commit**

```bash
git add src/main.py
git commit -m "feat(B1N-232): wire notification router and bot into main app"
```

---

## Task 9: Full Test Suite Run

- [ ] **Step 1: Run all tests**

```bash
uv run pytest -q
```

Expected: all tests pass, including existing tests (no regressions).

- [ ] **Step 2: Run linter**

```bash
uv run ruff check src/notifications/ src/api/notifications.py src/bots/notification_bot.py src/models/notification.py tests/test_notifications_api.py tests/test_notification_bot.py tests/test_settler_emails.py tests/test_email_service.py
```

Expected: no errors

- [ ] **Step 3: Run formatter**

```bash
uv run ruff format src/notifications/ src/api/notifications.py src/bots/notification_bot.py src/models/notification.py tests/test_notifications_api.py tests/test_notification_bot.py tests/test_settler_emails.py tests/test_email_service.py
```

- [ ] **Step 4: Fix any issues and commit**

```bash
git add -A
git commit -m "chore(B1N-232): lint and format notification code"
```

- [ ] **Step 5: Apply migration to Supabase**

```bash
cd /Users/rafa/Desktop/SoftwareDevelopment/personal/options/backend
supabase db push
```

Or apply manually via Supabase dashboard SQL editor using `supabase/migrations/20260326_user_emails.sql`.

- [ ] **Step 6: Add env vars to .env**

Add to `.env`:

```
RESEND_API_KEY=re_your_key_here
UNSUBSCRIBE_SECRET=<generate-a-random-32-char-string>
```

Generate the secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
