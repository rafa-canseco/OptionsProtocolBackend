-- B1N-256: Add chain column for dual-chain (Base + Solana) support.
-- Default 'base' preserves all existing data without breakage.

-- order_events
ALTER TABLE order_events
  ADD COLUMN IF NOT EXISTS chain TEXT NOT NULL DEFAULT 'base';

CREATE INDEX IF NOT EXISTS idx_order_events_chain
  ON order_events (chain);

-- mm_quotes
ALTER TABLE mm_quotes
  ADD COLUMN IF NOT EXISTS chain TEXT NOT NULL DEFAULT 'base';

CREATE INDEX IF NOT EXISTS idx_mm_quotes_chain
  ON mm_quotes (chain);

-- available_otokens
ALTER TABLE available_otokens
  ADD COLUMN IF NOT EXISTS chain TEXT NOT NULL DEFAULT 'base';

CREATE INDEX IF NOT EXISTS idx_available_otokens_chain
  ON available_otokens (chain);

-- mm_capacity
ALTER TABLE mm_capacity
  ADD COLUMN IF NOT EXISTS chain TEXT NOT NULL DEFAULT 'base';

CREATE INDEX IF NOT EXISTS idx_mm_capacity_chain
  ON mm_capacity (chain);

-- indexer_state: convert from singleton id=1 to one row per chain.
-- Add chain column, migrate existing row, then swap PK.
ALTER TABLE indexer_state
  ADD COLUMN IF NOT EXISTS chain TEXT;

UPDATE indexer_state SET chain = 'base' WHERE chain IS NULL;

ALTER TABLE indexer_state
  ALTER COLUMN chain SET NOT NULL,
  ALTER COLUMN chain SET DEFAULT 'base';

-- Drop the old CHECK constraint (id = 1) and PK, replace with chain PK.
-- The CHECK name varies by environment; use DO block to be safe.
DO $$
BEGIN
  -- Drop CHECK constraint on id if it exists
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'indexer_state'
      AND constraint_type = 'CHECK'
  ) THEN
    EXECUTE (
      SELECT 'ALTER TABLE indexer_state DROP CONSTRAINT ' || constraint_name
      FROM information_schema.table_constraints
      WHERE table_name = 'indexer_state'
        AND constraint_type = 'CHECK'
      LIMIT 1
    );
  END IF;

  -- Drop old PK if it exists
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'indexer_state'
      AND constraint_type = 'PRIMARY KEY'
  ) THEN
    EXECUTE (
      SELECT 'ALTER TABLE indexer_state DROP CONSTRAINT ' || constraint_name
      FROM information_schema.table_constraints
      WHERE table_name = 'indexer_state'
        AND constraint_type = 'PRIMARY KEY'
      LIMIT 1
    );
  END IF;
END $$;

ALTER TABLE indexer_state
  ADD PRIMARY KEY (chain);
