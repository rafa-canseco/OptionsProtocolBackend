-- Yield attribution tables for Aave yield tracking and weekly distribution.
-- Zero risk to existing data: 3 new tables + 1 new indexer_state row.

CREATE TABLE IF NOT EXISTS yield_positions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_address text NOT NULL,
    vault_id bigint NOT NULL,
    asset text NOT NULL,
    collateral_amount bigint NOT NULL,
    deposited_at timestamptz NOT NULL,
    settled_at timestamptz,
    block_number bigint NOT NULL,
    tx_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_address, vault_id, asset, tx_hash)
);

CREATE INDEX IF NOT EXISTS idx_yield_positions_user
    ON yield_positions (user_address);
CREATE INDEX IF NOT EXISTS idx_yield_positions_asset_active
    ON yield_positions (asset, deposited_at) WHERE settled_at IS NULL;

CREATE TABLE IF NOT EXISTS yield_distributions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    harvest_tx_hash text NOT NULL UNIQUE,
    asset text NOT NULL,
    total_yield bigint NOT NULL,
    platform_fee bigint NOT NULL DEFAULT 0,
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    distributed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS yield_allocations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    distribution_id uuid NOT NULL REFERENCES yield_distributions(id),
    position_id uuid NOT NULL REFERENCES yield_positions(id),
    user_address text NOT NULL,
    asset text NOT NULL,
    amount bigint NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered')),
    airdrop_tx_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_yield_allocations_user
    ON yield_allocations (user_address);
CREATE INDEX IF NOT EXISTS idx_yield_allocations_distribution
    ON yield_allocations (distribution_id);
CREATE INDEX IF NOT EXISTS idx_yield_allocations_pending
    ON yield_allocations (status) WHERE status = 'pending';

-- Separate state table for yield indexer (indexer_state has CHECK id=1)
CREATE TABLE IF NOT EXISTS yield_indexer_state (
    id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_indexed_block bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO yield_indexer_state (id, last_indexed_block)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;
