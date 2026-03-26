# B1N-232: Email Notifications for Expiry Reminder + Settlement Result

## Problem

Users have no way to get notified about upcoming expiry or settlement results. The frontend will offer email opt-in; this ticket handles the backend: verification flow, email sends, and integration with the settlement pipeline.

## Architecture

Hybrid approach with two integration points:

1. **Reminder emails** — a dedicated `notification_bot.py` runs every 30 minutes, queries positions expiring in ~24h, and sends reminders via Resend batch API. Independent from settlement timing.
2. **Settlement result emails** — fired inline from `expiry_settler.py` after `settle_once()` completes. Fire-and-forget: failures are logged but never block settlement.

Email sending is wrapped in `src/services/email.py` which handles Resend API calls and template rendering. All emails use branded HTML templates.

## Database

### New table: `user_emails`

```sql
CREATE TABLE user_emails (
    wallet_address TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    verified_at TIMESTAMPTZ,
    verification_code TEXT,
    code_expires_at TIMESTAMPTZ,
    unsubscribed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_user_emails_verified
    ON user_emails (wallet_address)
    WHERE verified_at IS NOT NULL AND unsubscribed_at IS NULL;
```

### Modified table: `order_events` — two new columns

```sql
ALTER TABLE order_events ADD COLUMN reminder_sent_at TIMESTAMPTZ;
ALTER TABLE order_events ADD COLUMN result_sent_at TIMESTAMPTZ;
```

These enforce the "max 2 emails per position" rule. Before sending, the bot checks `IS NULL` on the relevant column.

## New files

| File | Purpose |
|---|---|
| `src/api/notifications.py` | 3 API endpoints + 1 GET unsubscribe |
| `src/services/email.py` | Resend wrapper: send_verification, send_reminder_batch, send_result_batch |
| `src/services/email_templates.py` | Branded HTML templates for all 3 email types |
| `src/bots/notification_bot.py` | Reminder check every 30 min |
| `supabase/migrations/20260326_user_emails.sql` | Migration for new table + columns |

## Modified files

| File | Change |
|---|---|
| `src/config.py` | Add `resend_api_key`, `email_from`, `unsubscribe_secret` settings |
| `src/main.py` | Include notifications router, start notification bot in lifespan |
| `src/bots/expiry_settler.py` | Call `send_result_batch()` after settlement (try/except, non-blocking) |
| `pyproject.toml` | Add `resend` dependency |

## API Endpoints

### `POST /notifications/email` — Submit email for verification

- Body: `{ "wallet_address": "0x...", "email": "user@example.com" }`
- Validates wallet format (regex) + email format (pydantic EmailStr)
- Rate limit: 3 per wallet per hour AND 10 per IP per hour (in-memory, same pattern as waitlist). Dual limit prevents both wallet-spoofing abuse and IP-based email bombing.
- If wallet already has a verified email, upsert overwrites the email, clears `verified_at`, and starts a new verification flow. This lets users change their email.
- If wallet was previously unsubscribed, clears `unsubscribed_at` (re-subscribe on new verification).
- Generates 6-digit code, upserts into `user_emails`, sends via Resend
- Returns `{ "ok": true }`

### `POST /notifications/verify` — Verify code

- Body: `{ "wallet_address": "0x...", "code": "384921" }`
- Checks code matches + `code_expires_at` not passed (10 min TTL)
- Sets `verified_at = now()`, clears `verification_code` and `code_expires_at`
- Returns `{ "ok": true, "verified": true }`

### `GET /notifications/status?wallet=0x...` — Frontend state check

- Returns `{ "has_email": bool, "verified": bool, "unsubscribed": bool }`
- No auth (wallet addresses are public on-chain; only booleans exposed, not the email)

### `GET /notifications/unsubscribe?token=...` — Email link unsubscribe

- Token = HMAC-SHA256(wallet_address, `unsubscribe_secret`), hex-encoded
- URL format: `/notifications/unsubscribe?token=<hmac>&wallet=<address>`
- Verifies HMAC, sets `unsubscribed_at = now()`
- Returns simple HTML page: "You've been unsubscribed from b1nary notifications."
- Also handles POST to the same URL (returns 200 OK) — required by RFC 8058 for `List-Unsubscribe-Post` header support. Gmail and other clients send a POST, not GET, for one-click unsubscribe.

No unauthenticated `POST /notifications/unsubscribe` — all unsubscribe requests (GET or POST) require the HMAC token, preventing unauthorized unsubscription.

## Email Types

### 1. Verification code

- From: `b1nary <notifications@b1nary.app>`
- Subject: "Your b1nary verification code"
- Body: branded template with 6-digit code, "expires in 10 minutes" note
- No unsubscribe link (transactional, one-time)

### 2. Reminder (~24h before expiry)

- Subject: "Your {ASSET} ${STRIKE} {put/call} expires tomorrow at 8:00 AM UTC"
- Body: branded template with position details (asset, strike, type, premium earned), link to app
- Includes `List-Unsubscribe` header + footer unsubscribe link

### 3. Settlement result

Three variants based on outcome:

- **OTM:** "Your ${COLLATERAL} is back + you kept ${PREMIUM}. [Earn again]"
- **ITM PUT:** "You bought {AMOUNT} {ASSET} at ${STRIKE}. [View position]"
- **ITM CALL:** "You sold {AMOUNT} {ASSET} at ${STRIKE}. [View position]"
- Includes `List-Unsubscribe` header + footer unsubscribe link

All emails include the `List-Unsubscribe` and `List-Unsubscribe-Post` headers per Resend docs / CAN-SPAM compliance.

## Notification Bot (`notification_bot.py`)

Runs every 30 minutes via asyncio loop (same pattern as `circuit_breaker_bot`).

### Reminder flow

1. Query `order_events` where:
   - `expiry` BETWEEN `now + 20h` AND `now + 28h`
   - `reminder_sent_at IS NULL`
   - `is_settled IS NOT TRUE`
   - `created_at < now - 2h` (skip positions created in the last 2 hours to avoid sending a reminder for a position the user just opened on a 1-day expiry)
2. Collect distinct `user_address` values from matching positions
3. Query `user_emails` for those wallets where `verified_at IS NOT NULL` AND `unsubscribed_at IS NULL`
4. Build reminder emails per position (grouped by wallet for batch send)
5. Send via `resend.Batch.send()` (handles up to 500 emails per call)
6. On success, mark `reminder_sent_at = now()` on each position in `order_events`

### Failure handling

- If Resend batch fails entirely, log and retry next cycle (30 min later)
- If Resend batch partially succeeds, mark `reminder_sent_at` only for successfully sent emails (check Resend batch response per-email). Positions that failed remain `IS NULL` and will be retried.
- Positions with `reminder_sent_at IS NULL` and still in the 20-28h window will be picked up again
- If the window passes (< 20h to expiry), the reminder is skipped — no stale reminders
- If a template fails to render for a single position (e.g., missing strike_price), skip that position and continue with the rest of the batch

## Settlement Result Integration (`expiry_settler.py`)

At the end of `settle_once()`, **inside** the function after all ITM/OTM processing (the variables `settled_positions`, `itm_positions`, and `otm_positions` are local to `settle_once()`):

```python
try:
    await _send_settlement_emails(settled_positions, itm_positions, otm_positions)
except Exception:
    logger.exception("Settlement emails failed (non-blocking)")
```

### Result email flow

1. Collect all `user_address` values from settled positions
2. Query `user_emails` for verified + not unsubscribed wallets
3. Filter to positions where `result_sent_at IS NULL`
4. Build OTM or ITM email content per position
5. Send via `resend.Batch.send()`
6. Mark `result_sent_at = now()` per position

Settlement is never affected by email failures — the entire block is wrapped in try/except.

## Configuration (`src/config.py`)

New settings:

```python
resend_api_key: str = ""          # RESEND_API_KEY env var
email_from: str = "b1nary <notifications@b1nary.app>"
unsubscribe_secret: str = ""      # HMAC secret for unsubscribe tokens
notification_check_interval_seconds: int = 1800  # 30 min (minimum 60s enforced at startup)
```

The notification bot and email sends are only active when `resend_api_key` is set (same guard pattern as on-chain bots with `batch_settler_address`).

## Anti-spam / Security

- **Rate limiting:** 3 per wallet per hour + 10 per IP per hour (in-memory, per-worker). Dual limit prevents wallet-spoofing and IP-based abuse.
- **Code expiry:** Verification codes expire in 10 minutes
- **Unsubscribe tokens:** HMAC-SHA256 signed with `unsubscribe_secret` — cannot forge
- **Dedup:** `reminder_sent_at` / `result_sent_at` columns prevent duplicate sends per position
- **Unsubscribe requires HMAC token:** Both GET and POST to the unsubscribe URL require a valid HMAC-signed token — cannot forge or unsubscribe others. POST support required by RFC 8058 (Gmail one-click unsubscribe).
- **Transactional only:** No marketing emails, no bulk sends
- **Graceful degradation:** If Resend is down or `resend_api_key` is empty, the system operates normally without emails

## Scale

- Built for up to ~500 positions per expiry cycle
- Uses `resend.Batch.send()` for reminder and result emails
- Sequential sends only for verification (1 email at a time)

## Dependencies

- `resend` Python SDK (new dependency in `pyproject.toml`)
- No other new dependencies (HMAC is stdlib `hmac` + `hashlib`)
