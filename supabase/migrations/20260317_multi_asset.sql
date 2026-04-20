-- Multi-asset support: add underlying/asset columns
-- Run on STAGING only. Do NOT run on production.

-- 1. available_otokens: add underlying column (token address)
ALTER TABLE available_otokens
  ADD COLUMN IF NOT EXISTS underlying text;

-- Backfill existing rows with WETH address (Base mainnet)
UPDATE available_otokens
  SET underlying = '0x4200000000000000000000000000000000000006'
  WHERE underlying IS NULL;

ALTER TABLE available_otokens
  ALTER COLUMN underlying SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_available_otokens_underlying
  ON available_otokens (underlying);

-- 2. mm_quotes: add asset column
ALTER TABLE mm_quotes
  ADD COLUMN IF NOT EXISTS asset text NOT NULL DEFAULT 'eth';

CREATE INDEX IF NOT EXISTS idx_mm_quotes_asset
  ON mm_quotes (asset);

-- 3. mm_capacity: add asset column and change PK to (mm_address, asset)
ALTER TABLE mm_capacity
  ADD COLUMN IF NOT EXISTS asset text NOT NULL DEFAULT 'eth';

ALTER TABLE mm_capacity
  DROP CONSTRAINT IF EXISTS mm_capacity_pkey;

ALTER TABLE mm_capacity
  ADD CONSTRAINT mm_capacity_pkey PRIMARY KEY (mm_address, asset);

-- 4. order_events: add asset column (populated by event indexer)
ALTER TABLE order_events
  ADD COLUMN IF NOT EXISTS asset text NOT NULL DEFAULT 'eth';

CREATE INDEX IF NOT EXISTS idx_order_events_asset
  ON order_events (asset);
