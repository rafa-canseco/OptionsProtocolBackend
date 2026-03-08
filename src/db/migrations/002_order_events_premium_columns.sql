-- Add gross_premium, net_premium, protocol_fee to order_events.
-- These columns were added to the event indexer but missing from the table.

alter table order_events add column if not exists gross_premium numeric;
alter table order_events add column if not exists net_premium numeric;
alter table order_events add column if not exists protocol_fee numeric;
