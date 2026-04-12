-- Solana event indexer state table.
-- Tracks slot/signature cursor independently from Base indexer_state.

CREATE TABLE IF NOT EXISTS solana_indexer_state (
  chain TEXT NOT NULL DEFAULT 'solana',
  program_id TEXT NOT NULL,
  last_signature TEXT,
  last_slot BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chain, program_id),
  CONSTRAINT chk_solana_indexer_state_chain
    CHECK (chain IN ('solana'))
);
