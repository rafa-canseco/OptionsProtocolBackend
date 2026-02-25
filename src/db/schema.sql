-- Run this in the Supabase SQL editor to create the tables
-- ============================================================

-- Legacy tables (kept for backwards compatibility, no longer written to by API)

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

-- ============================================================
-- New tables: on-chain event indexing
-- ============================================================

-- Indexed OrderExecuted events from BatchSettler.executeOrder()
create table if not exists order_events (
  id uuid primary key default gen_random_uuid(),
  tx_hash text not null unique,
  block_number bigint not null,
  log_index integer not null,
  user_address text not null,
  otoken_address text not null,
  amount numeric not null,
  premium numeric not null,
  collateral numeric not null,
  vault_id integer not null,
  -- Denormalized oToken metadata
  strike_price numeric,
  expiry bigint,
  is_put boolean,
  -- Settlement tracking
  is_settled boolean not null default false,
  settled_at timestamptz,
  settlement_tx_hash text,
  -- Indexing metadata
  indexed_at timestamptz not null default now()
);

create index if not exists idx_order_events_user on order_events(user_address);
create index if not exists idx_order_events_otoken on order_events(otoken_address);
create index if not exists idx_order_events_block on order_events(block_number);
create index if not exists idx_order_events_expiry on order_events(expiry);
create index if not exists idx_order_events_unsettled
  on order_events(is_settled) where is_settled = false;

-- Singleton row tracking last indexed block (for resumability)
create table if not exists indexer_state (
  id integer primary key default 1 check (id = 1),
  last_indexed_block bigint not null default 0,
  updated_at timestamptz not null default now()
);

insert into indexer_state (last_indexed_block) values (0)
  on conflict (id) do nothing;

-- ============================================================
-- Waitlist
-- ============================================================

-- ============================================================
-- Weekly aggregation (populated by weekly_aggregator bot)
-- ============================================================

create table if not exists user_weekly_results (
  id uuid primary key default gen_random_uuid(),
  user_address text not null,
  week_start text not null,
  week_end text not null,
  positions_opened integer not null,
  total_simulated_premium numeric not null,
  assignments integer not null,
  simulated_pnl numeric not null,
  cumulative_pnl numeric not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_address, week_start)
);

create table if not exists weekly_reports (
  id uuid primary key default gen_random_uuid(),
  week_start text not null unique,
  week_end text not null,
  total_users integer not null,
  total_positions integer not null,
  total_simulated_premium numeric not null,
  total_assignments integer not null,
  eth_open numeric not null,
  eth_close numeric not null,
  eth_high numeric not null,
  eth_low numeric not null,
  narrative_data jsonb default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- ============================================================
-- Waitlist
-- ============================================================

create table if not exists waitlist (
  id bigint generated always as identity primary key,
  email text not null unique,
  created_at timestamptz not null default now()
);
