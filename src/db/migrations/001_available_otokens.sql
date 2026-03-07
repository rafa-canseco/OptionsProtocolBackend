-- Migration: Create available_otokens table
-- Run in Supabase SQL Editor for the target project
-- Required before otoken_manager bot can write discovered oTokens

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
