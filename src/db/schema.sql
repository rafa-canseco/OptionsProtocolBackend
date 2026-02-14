-- Run this in the Supabase SQL editor to create the tables
-- Execute in order: batches first, then orders (FK dependency)

create table if not exists batches (
  id uuid primary key default gen_random_uuid(),
  status text not null default 'pending'
    check (status in ('pending', 'executing', 'settled', 'failed')),
  order_count integer not null default 0,
  total_premium numeric not null default 0,
  tx_hash text,
  created_at timestamptz not null default now(),
  settled_at timestamptz
);

create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  user_address text not null,
  option_type text not null check (option_type in ('call', 'put')),
  strike numeric not null,
  expiry_days integer not null,
  premium numeric not null,
  spot_at_lock numeric not null,
  iv_at_lock numeric not null,
  status text not null default 'pending'
    check (status in ('pending', 'batched', 'settled', 'expired', 'failed')),
  batch_id uuid references batches(id),
  tx_hash text,
  created_at timestamptz not null default now(),
  settled_at timestamptz
);

create index if not exists idx_orders_user on orders(user_address);
create index if not exists idx_orders_status on orders(status);
create index if not exists idx_orders_batch on orders(batch_id);
create index if not exists idx_batches_status on batches(status);
