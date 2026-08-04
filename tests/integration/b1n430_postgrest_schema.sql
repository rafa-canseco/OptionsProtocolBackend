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

CREATE TABLE public.b1nary_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT NOT NULL,
    username_normalized TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.b1nary_account_members (
    account_id UUID NOT NULL REFERENCES public.b1nary_accounts(id) ON DELETE CASCADE,
    privy_user_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'owner',
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, privy_user_id)
);

CREATE TABLE public.b1nary_wallets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES public.b1nary_accounts(id) ON DELETE CASCADE,
    privy_user_id TEXT,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    address_normalized TEXT NOT NULL,
    wallet_type TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'trading',
    wallet_client_type TEXT,
    verification_message TEXT,
    verification_signature TEXT,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain, address_normalized)
);

CREATE INDEX idx_b1n430_fixture_members_account
    ON public.b1nary_account_members (account_id);
CREATE INDEX idx_b1n430_fixture_wallets_account
    ON public.b1nary_wallets (account_id);
