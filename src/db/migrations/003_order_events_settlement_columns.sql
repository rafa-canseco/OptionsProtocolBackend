-- Add settlement tracking columns to order_events.
-- Used by expiry_settler bot and the /positions endpoint.

alter table order_events
  add column if not exists settlement_type text;

alter table order_events
  add column if not exists is_itm boolean;

alter table order_events
  add column if not exists expiry_price numeric;

alter table order_events
  add column if not exists delivered_asset text;

alter table order_events
  add column if not exists delivered_amount numeric;

alter table order_events
  add column if not exists delivery_tx_hash text;
