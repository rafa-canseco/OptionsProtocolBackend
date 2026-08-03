CREATE TABLE public.order_events (
    id UUID PRIMARY KEY,
    tx_hash TEXT NOT NULL UNIQUE,
    block_number BIGINT NOT NULL UNIQUE,
    user_address TEXT NOT NULL,
    collateral_usd DOUBLE PRECISION,
    premium NUMERIC NOT NULL,
    gross_premium NUMERIC,
    net_premium NUMERIC,
    is_put BOOLEAN,
    asset TEXT,
    indexed_at TIMESTAMPTZ NOT NULL,
    expiry BIGINT,
    is_settled BOOLEAN NOT NULL DEFAULT FALSE,
    is_itm BOOLEAN,
    settled_at TIMESTAMPTZ
);

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT SELECT ON public.order_events TO service_role;
