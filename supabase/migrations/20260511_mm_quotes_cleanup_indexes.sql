-- Keep mm_quotes cleanup fast as market makers refresh signed quotes.
-- Stale quotes are not trade history; canonical fills are stored in order_events.

CREATE INDEX IF NOT EXISTS idx_mm_quotes_cleanup_active
  ON mm_quotes (mm_address, chain, is_active);

CREATE INDEX IF NOT EXISTS idx_mm_quotes_cleanup_deadline
  ON mm_quotes (mm_address, chain, deadline);

CREATE INDEX IF NOT EXISTS idx_mm_quotes_cleanup_expiry
  ON mm_quotes (mm_address, chain, expiry);
