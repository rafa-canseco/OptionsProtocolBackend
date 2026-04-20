-- Add nullable group_id column to order_events for linking range positions.
-- A range order produces two independent positions (put + call);
-- group_id lets the frontend display them as a single unit.

ALTER TABLE order_events
  ADD COLUMN IF NOT EXISTS group_id uuid;

CREATE INDEX IF NOT EXISTS idx_order_events_group_id
  ON order_events (group_id) WHERE group_id IS NOT NULL;
