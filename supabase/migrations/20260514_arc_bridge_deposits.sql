-- B1N-333: Arc MetaVault deposit support for CCTP relayer.

ALTER TABLE bridge_jobs
  DROP CONSTRAINT IF EXISTS bridge_jobs_source_chain_check,
  DROP CONSTRAINT IF EXISTS bridge_jobs_dest_chain_check;

ALTER TABLE bridge_jobs
  ADD CONSTRAINT bridge_jobs_source_chain_check
    CHECK (source_chain IN ('base', 'solana', 'arc')),
  ADD CONSTRAINT bridge_jobs_dest_chain_check
    CHECK (dest_chain IN ('base', 'solana', 'arc'));

ALTER TABLE bridge_jobs
  ADD COLUMN IF NOT EXISTS arc_receive_tx_hash TEXT,
  ADD COLUMN IF NOT EXISTS arc_finalize_tx_hash TEXT,
  ADD COLUMN IF NOT EXISTS gross_amount_usdc TEXT,
  ADD COLUMN IF NOT EXISTS circle_fee_usdc TEXT,
  ADD COLUMN IF NOT EXISTS net_amount_usdc TEXT,
  ADD COLUMN IF NOT EXISTS receiver TEXT;

ALTER TABLE capital_movement_intents
  ADD COLUMN IF NOT EXISTS onchain_intent_id TEXT,
  ADD COLUMN IF NOT EXISTS arc_receive_tx_hash TEXT,
  ADD COLUMN IF NOT EXISTS arc_finalize_tx_hash TEXT,
  ADD COLUMN IF NOT EXISTS gross_amount_usdc TEXT,
  ADD COLUMN IF NOT EXISTS circle_fee_usdc TEXT,
  ADD COLUMN IF NOT EXISTS net_amount_usdc TEXT;

CREATE INDEX IF NOT EXISTS idx_capital_movement_intents_onchain_intent
  ON capital_movement_intents (onchain_intent_id)
  WHERE onchain_intent_id IS NOT NULL;
