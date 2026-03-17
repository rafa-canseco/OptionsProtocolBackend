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

-- Index for filtering oTokens by underlying
CREATE INDEX IF NOT EXISTS idx_available_otokens_underlying
  ON available_otokens (underlying);

-- 2. mm_quotes: add asset column
ALTER TABLE mm_quotes
  ADD COLUMN IF NOT EXISTS asset text NOT NULL DEFAULT 'eth';

CREATE INDEX IF NOT EXISTS idx_mm_quotes_asset
  ON mm_quotes (asset);
