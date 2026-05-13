-- B1N-323: MetaVault capital movement intent tracking.
--
-- This table is the business-level lifecycle for USDC movement. Existing
-- bridge_jobs remain the technical CCTP execution record and are linked when a
-- movement can reuse the current bridge relayer.

CREATE TABLE IF NOT EXISTS capital_movement_intents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  intent_type TEXT NOT NULL
    CHECK (intent_type IN ('deposit', 'deployment', 'return')),
  movement_reason TEXT NOT NULL DEFAULT 'initial_deployment'
    CHECK (movement_reason IN (
      'user_deposit',
      'initial_deployment',
      'rotation',
      'return_to_arc',
      'return_to_idle',
      'withdrawal'
    )),
  bucket_id TEXT,
  receiver TEXT,
  source_chain TEXT NOT NULL
    CHECK (source_chain IN ('arc', 'base', 'solana')),
  source_account TEXT NOT NULL,
  source_tx TEXT,
  destination_chain TEXT NOT NULL
    CHECK (destination_chain IN ('arc', 'base', 'solana')),
  destination_account TEXT NOT NULL,
  destination_tx TEXT,
  amount_usdc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending',
      'deposit_received',
      'bridging',
      'waiting_to_be_deployed',
      'deployment_in_flight',
      'deployed',
      'premium_earned',
      'assigned_rotating',
      'returning_to_arc',
      'returned_to_usdc',
      'claimable',
      'completed',
      'failed',
      'retryable'
    )),
  bridge_job_id UUID REFERENCES bridge_jobs(id),
  idempotency_key TEXT,
  completed_at TIMESTAMPTZ,
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_capital_movement_intents_idempotency
  ON capital_movement_intents (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_capital_movement_intents_bucket
  ON capital_movement_intents (bucket_id);

CREATE INDEX IF NOT EXISTS idx_capital_movement_intents_bridge_job
  ON capital_movement_intents (bridge_job_id);

CREATE INDEX IF NOT EXISTS idx_capital_movement_intents_status
  ON capital_movement_intents (status);

CREATE INDEX IF NOT EXISTS idx_capital_movement_intents_location
  ON capital_movement_intents (source_chain, destination_chain);
