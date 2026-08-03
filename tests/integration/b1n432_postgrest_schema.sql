CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE public.order_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tx_hash TEXT NOT NULL UNIQUE,
    block_number BIGINT NOT NULL,
    log_index INTEGER NOT NULL,
    chain TEXT NOT NULL DEFAULT 'base',
    user_address TEXT NOT NULL,
    mm_address TEXT,
    otoken_address TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    premium NUMERIC NOT NULL,
    gross_premium NUMERIC,
    net_premium NUMERIC,
    protocol_fee NUMERIC,
    collateral NUMERIC NOT NULL,
    collateral_usd DOUBLE PRECISION,
    vault_id INTEGER NOT NULL,
    strike_price NUMERIC,
    expiry BIGINT,
    is_put BOOLEAN,
    is_settled BOOLEAN NOT NULL DEFAULT FALSE,
    settled_at TIMESTAMPTZ,
    settlement_tx_hash TEXT,
    settlement_type TEXT,
    is_itm BOOLEAN,
    expiry_price NUMERIC,
    delivered_asset TEXT,
    delivered_amount NUMERIC,
    delivery_tx_hash TEXT,
    group_id UUID,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    asset TEXT NOT NULL DEFAULT 'eth'
);

CREATE INDEX idx_b1n432_fixture_order_events_user
    ON public.order_events (user_address);

CREATE TABLE public.yield_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_address TEXT NOT NULL,
    vault_id BIGINT NOT NULL,
    asset TEXT NOT NULL,
    collateral_amount BIGINT NOT NULL,
    deposited_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ,
    block_number BIGINT NOT NULL,
    tx_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_address, vault_id, asset, tx_hash)
);

CREATE TABLE public.yield_distributions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    harvest_tx_hash TEXT NOT NULL UNIQUE,
    asset TEXT NOT NULL,
    total_yield BIGINT NOT NULL,
    platform_fee BIGINT NOT NULL DEFAULT 0,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    distributed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.yield_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    distribution_id UUID NOT NULL REFERENCES public.yield_distributions(id),
    position_id UUID NOT NULL REFERENCES public.yield_positions(id),
    user_address TEXT NOT NULL,
    asset TEXT NOT NULL,
    amount BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered')),
    airdrop_tx_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
