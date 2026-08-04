CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE public.order_events (
    id UUID PRIMARY KEY,
    tx_hash TEXT NOT NULL UNIQUE,
    block_number BIGINT NOT NULL UNIQUE,
    user_address TEXT NOT NULL,
    amount NUMERIC NOT NULL,
    premium NUMERIC NOT NULL,
    gross_premium NUMERIC,
    net_premium NUMERIC,
    strike_price NUMERIC,
    collateral_usd DOUBLE PRECISION,
    is_put BOOLEAN,
    asset TEXT,
    indexed_at TIMESTAMPTZ NOT NULL,
    expiry BIGINT,
    is_settled BOOLEAN NOT NULL DEFAULT FALSE,
    is_itm BOOLEAN,
    settled_at TIMESTAMPTZ
);

CREATE TABLE public.user_weekly_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_address TEXT NOT NULL,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    positions_opened INTEGER NOT NULL,
    total_simulated_premium NUMERIC NOT NULL,
    assignments INTEGER NOT NULL,
    simulated_pnl NUMERIC NOT NULL,
    cumulative_pnl NUMERIC NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_address, week_start)
);

CREATE TABLE public.weekly_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    week_start TEXT NOT NULL UNIQUE,
    week_end TEXT NOT NULL,
    total_users INTEGER NOT NULL,
    total_positions INTEGER NOT NULL,
    total_simulated_premium NUMERIC NOT NULL,
    total_assignments INTEGER NOT NULL,
    eth_open NUMERIC NOT NULL,
    eth_close NUMERIC NOT NULL,
    eth_high NUMERIC NOT NULL,
    eth_low NUMERIC NOT NULL,
    narrative_data JSONB DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT SELECT ON public.order_events TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_weekly_results TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.weekly_reports TO service_role;
