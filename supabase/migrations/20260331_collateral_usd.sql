-- Add collateral_usd column to order_events
-- Stores the USD value of collateral frozen at position creation time
-- Column is nullable: existing rows will be backfilled by a separate script

ALTER TABLE order_events
  ADD COLUMN IF NOT EXISTS collateral_usd float;
