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
  mm_address text,
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

create index if not exists idx_order_events_mm on order_events(mm_address);
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

-- ============================================================
-- Market Maker quotes (EIP-712 signed, stored off-chain)
-- ============================================================

create table if not exists mm_quotes (
  id uuid primary key default gen_random_uuid(),
  mm_address text not null,
  otoken_address text not null,
  bid_price numeric not null,
  deadline bigint not null,
  quote_id text not null,
  max_amount numeric not null,
  maker_nonce bigint not null,
  signature text not null,
  -- Denormalized oToken metadata (for display / filtering)
  strike_price numeric,
  expiry bigint,
  is_put boolean,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  unique (mm_address, quote_id)
);

create index if not exists idx_mm_quotes_active
  on mm_quotes (is_active, deadline) where is_active = true;
create index if not exists idx_mm_quotes_otoken
  on mm_quotes (otoken_address) where is_active = true;
create index if not exists idx_mm_quotes_mm
  on mm_quotes (mm_address) where is_active = true;

-- ============================================================
-- Market Maker API keys
-- ============================================================

create table if not exists mm_api_keys (
  id uuid primary key default gen_random_uuid(),
  mm_address text not null unique,
  api_key text not null unique,
  label text,
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);

-- ============================================================
-- Available oTokens (populated by otoken_manager bot)
-- ============================================================

create table if not exists available_otokens (
  id uuid primary key default gen_random_uuid(),
  otoken_address text not null unique,
  strike_price numeric not null,
  expiry bigint not null,
  is_put boolean not null,
  collateral_asset text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_available_otokens_expiry
  on available_otokens(expiry);
