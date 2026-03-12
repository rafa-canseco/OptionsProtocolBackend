-- MM capacity reports (one row per MM, upserted on each report)
create table if not exists mm_capacity (
  mm_address text primary key,
  asset text not null default 'ETH',
  capacity_eth numeric not null,
  capacity_usd numeric not null,
  premium_pool_usd numeric,
  hedge_pool_usd numeric,
  hedge_pool_withdrawable_usd numeric,
  leverage integer,
  open_positions_count integer,
  open_positions_notional_usd numeric,
  status text not null default 'active'
    check (status in ('active', 'degraded', 'full')),
  reported_at timestamptz not null default now()
);
