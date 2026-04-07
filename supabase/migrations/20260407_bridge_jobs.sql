-- B1N-274: Bridge Relayer job tracking for CCTP V2 cross-chain transfers.

CREATE TABLE IF NOT EXISTS bridge_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  source_chain TEXT NOT NULL CHECK (source_chain IN ('base', 'solana')),
  dest_chain TEXT NOT NULL CHECK (dest_chain IN ('base', 'solana')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending', 'attesting', 'minting',
      'trading', 'completed', 'mint_completed',
      'failed', 'mint_completed_trade_failed'
    )),
  burn_tx_hash TEXT NOT NULL,
  burn_amount TEXT NOT NULL,
  mint_recipient TEXT NOT NULL,
  quote_id TEXT,
  signed_trade_tx TEXT,
  attestation_message TEXT,
  attestation_signature TEXT,
  mint_tx_hash TEXT,
  trade_tx_hash TEXT,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bridge_jobs_status
  ON bridge_jobs (status);

CREATE INDEX IF NOT EXISTS idx_bridge_jobs_user
  ON bridge_jobs (user_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bridge_jobs_burn_tx
  ON bridge_jobs (burn_tx_hash);

-- Dedup: reject duplicate jobs for the same quote
CREATE UNIQUE INDEX IF NOT EXISTS idx_bridge_jobs_quote_id
  ON bridge_jobs (quote_id) WHERE quote_id IS NOT NULL;

-- Add bridge metadata to order_events
ALTER TABLE order_events
  ADD COLUMN IF NOT EXISTS bridge_job_id UUID,
  ADD COLUMN IF NOT EXISTS source_chain TEXT;
