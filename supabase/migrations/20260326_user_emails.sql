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
