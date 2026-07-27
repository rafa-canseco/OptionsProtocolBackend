ALTER TABLE v2_fund_registry
    ADD COLUMN strategy_kind TEXT NOT NULL DEFAULT 'csp',
    ADD COLUMN quote_asset TEXT,
    ADD COLUMN quote_asset_symbol TEXT,
    ADD COLUMN quote_asset_decimals INTEGER;

ALTER TABLE v2_fund_registry
    ADD CONSTRAINT v2_fund_registry_strategy_kind_check
        CHECK (strategy_kind IN ('csp', 'covered_call')),
    ADD CONSTRAINT v2_fund_registry_quote_asset_check CHECK (
        (
            strategy_kind = 'csp'
            AND quote_asset IS NULL
            AND quote_asset_symbol IS NULL
            AND quote_asset_decimals IS NULL
        )
        OR (
            strategy_kind = 'covered_call'
            AND quote_asset IS NOT NULL
            AND quote_asset = lower(quote_asset)
            AND quote_asset_symbol IS NOT NULL
            AND quote_asset_decimals BETWEEN 0 AND 255
        )
    );

ALTER TABLE v2_fund_contracts
    DROP CONSTRAINT IF EXISTS v2_fund_contracts_contract_role_check,
    DROP CONSTRAINT IF EXISTS v2_fund_contracts_check;

ALTER TABLE v2_fund_contracts
    ADD CONSTRAINT v2_fund_contracts_contract_role_check CHECK (
        contract_role IN (
            'fund_vault', 'fund_share', 'fund_accounting',
            'fund_flow_manager', 'strategy_manager', 'csp_adapter',
            'covered_call_adapter', 'controller', 'batch_settler',
            'claim_escrow', 'access_manager', 'address_book',
            'csp_valuator', 'covered_call_valuator', 'margin_pool',
            'nav_verifier', 'oracle', 'otoken_factory', 'swap_router',
            'whitelist'
        )
    ),
    ADD CONSTRAINT v2_fund_contracts_proxy_implementation_check CHECK (
        contract_role NOT IN (
            'fund_vault', 'fund_share', 'fund_accounting',
            'fund_flow_manager', 'strategy_manager', 'csp_adapter',
            'covered_call_adapter', 'controller', 'batch_settler'
        )
        OR implementation_address IS NOT NULL
    );

ALTER TABLE v2_csp_positions
    DROP CONSTRAINT IF EXISTS v2_csp_positions_lifecycle_check;

ALTER TABLE v2_csp_positions
    ADD CONSTRAINT v2_csp_positions_lifecycle_check CHECK (
        lifecycle IN (
            'open', 'awaiting_physical_delivery', 'settled_otm',
            'assigned', 'called_away', 'cash_fallback'
        )
    );

ALTER TABLE v2_fund_inventory
    DROP CONSTRAINT IF EXISTS v2_fund_inventory_bucket_check;

ALTER TABLE v2_fund_inventory
    ADD CONSTRAINT v2_fund_inventory_bucket_check CHECK (
        bucket IN (
            'strategy_accounted', 'assigned', 'claim_reserved',
            'transient_usdc'
        )
    );

ALTER TABLE v2_fund_state
    ADD COLUMN normalization_slippage_bps INTEGER NOT NULL DEFAULT 0
        CHECK (normalization_slippage_bps BETWEEN 0 AND 10000);

ALTER TABLE v2_csp_option_observations
    ADD COLUMN strategy_kind TEXT NOT NULL DEFAULT 'csp'
        CHECK (strategy_kind IN ('csp', 'covered_call'));

ALTER TABLE v2_csp_fair_value_marks
    ADD COLUMN strategy_kind TEXT NOT NULL DEFAULT 'csp'
        CHECK (strategy_kind IN ('csp', 'covered_call')),
    ADD COLUMN policy_reference TEXT,
    ADD COLUMN policy_sha256 TEXT CHECK (
        policy_sha256 IS NULL OR policy_sha256 ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT v2_fair_value_marks_covered_call_policy_check CHECK (
        strategy_kind = 'csp'
        OR (policy_reference IS NOT NULL AND policy_sha256 IS NOT NULL)
    );

CREATE TABLE v2_fund_strategy_positions (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    adapter_address TEXT NOT NULL,
    position_id NUMERIC(78, 0) NOT NULL,
    strategy_kind TEXT NOT NULL CHECK (
        strategy_kind IN ('csp', 'covered_call')
    ),
    protocol_vault_id NUMERIC(78, 0) NOT NULL,
    otoken_address TEXT NOT NULL,
    market_maker_address TEXT NOT NULL,
    option_amount NUMERIC(78, 0) NOT NULL,
    collateral NUMERIC(78, 0) NOT NULL,
    premium_earned NUMERIC(78, 0) NOT NULL,
    collateral_returned NUMERIC(78, 0) NOT NULL DEFAULT 0,
    settlement_payout NUMERIC(78, 0) NOT NULL DEFAULT 0,
    payment NUMERIC(78, 0) NOT NULL DEFAULT 0,
    assigned_weth NUMERIC(78, 0) NOT NULL DEFAULT 0,
    called_away_usdc NUMERIC(78, 0) NOT NULL DEFAULT 0,
    fallback_weth_recovered NUMERIC(78, 0) NOT NULL DEFAULT 0,
    mm_weth_payout NUMERIC(78, 0) NOT NULL DEFAULT 0,
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN (
            'open', 'awaiting_physical_delivery', 'settled_otm',
            'assigned', 'called_away', 'cash_fallback'
        )
    ),
    lifecycle_hash TEXT NOT NULL,
    strike_price_8 NUMERIC(78, 0),
    expiry_timestamp BIGINT,
    is_put BOOLEAN,
    opened_block BIGINT NOT NULL,
    settled_block BIGINT,
    PRIMARY KEY (chain_id, fund_address, adapter_address, position_id),
    UNIQUE (chain_id, fund_address, adapter_address, protocol_vault_id),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE INDEX v2_fund_strategy_positions_active_idx
    ON v2_fund_strategy_positions (
        chain_id, fund_address, strategy_kind, lifecycle
    )
    WHERE lifecycle IN ('open', 'awaiting_physical_delivery');

ALTER TABLE v2_fund_strategy_positions ENABLE ROW LEVEL SECURITY;

INSERT INTO v2_fund_strategy_positions (
    chain_id, fund_address, adapter_address, position_id, strategy_kind,
    protocol_vault_id, otoken_address, market_maker_address, option_amount,
    collateral, premium_earned, collateral_returned, settlement_payout,
    payment, assigned_weth, lifecycle, lifecycle_hash, opened_block,
    settled_block
)
SELECT
    chain_id, fund_address, adapter_address, position_id, 'csp',
    protocol_vault_id, otoken_address, market_maker_address, option_amount,
    collateral, premium_earned, collateral_returned, settlement_payout,
    payment, assigned_weth, lifecycle, lifecycle_hash, opened_block,
    settled_block
FROM v2_csp_positions
ON CONFLICT DO NOTHING;

ALTER FUNCTION v2_ingest_fund_window(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) RENAME TO v2_ingest_fund_window_b1n353;

CREATE OR REPLACE FUNCTION v2_ingest_fund_window(
    p_chain_id BIGINT,
    p_fund_address TEXT,
    p_indexer_name TEXT,
    p_from_block BIGINT,
    p_to_block BIGINT,
    p_last_block_hash TEXT,
    p_events JSONB,
    p_projection JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    state JSONB;
BEGIN
    PERFORM v2_ingest_fund_window_b1n353(
        p_chain_id, p_fund_address, p_indexer_name, p_from_block,
        p_to_block, p_last_block_hash, p_events, p_projection
    );

    state := p_projection->'fund_state'->0;
    IF state IS NOT NULL THEN
        UPDATE v2_fund_state SET
            normalization_slippage_bps =
                COALESCE((state->>'normalization_slippage_bps')::INTEGER, 0)
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    END IF;

    DELETE FROM v2_fund_strategy_positions
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;

    INSERT INTO v2_fund_strategy_positions (
        chain_id, fund_address, adapter_address, position_id, strategy_kind,
        protocol_vault_id, otoken_address, market_maker_address,
        option_amount, collateral, premium_earned, collateral_returned,
        settlement_payout, payment, assigned_weth, called_away_usdc,
        fallback_weth_recovered, mm_weth_payout, lifecycle, lifecycle_hash,
        strike_price_8, expiry_timestamp, is_put, opened_block, settled_block
    )
    SELECT
        x.chain_id, x.fund_address, x.adapter_address, x.position_id,
        x.strategy_kind, x.protocol_vault_id, x.otoken_address,
        x.market_maker_address, x.option_amount, x.collateral,
        x.premium_earned, x.collateral_returned, x.settlement_payout,
        x.payment, x.assigned_weth, x.called_away_usdc,
        x.fallback_weth_recovered, x.mm_weth_payout, x.lifecycle,
        x.lifecycle_hash, x.strike_price_8, x.expiry_timestamp, x.is_put,
        x.opened_block, x.settled_block
    FROM jsonb_to_recordset(
        COALESCE(p_projection->'positions', '[]'::jsonb)
    ) AS x(
        chain_id BIGINT, fund_address TEXT, adapter_address TEXT,
        position_id NUMERIC, strategy_kind TEXT, protocol_vault_id NUMERIC,
        otoken_address TEXT, market_maker_address TEXT,
        option_amount NUMERIC, collateral NUMERIC, premium_earned NUMERIC,
        collateral_returned NUMERIC, settlement_payout NUMERIC,
        payment NUMERIC, assigned_weth NUMERIC, called_away_usdc NUMERIC,
        fallback_weth_recovered NUMERIC, mm_weth_payout NUMERIC,
        lifecycle TEXT, lifecycle_hash TEXT, strike_price_8 NUMERIC,
        expiry_timestamp BIGINT, is_put BOOLEAN, opened_block BIGINT,
        settled_block BIGINT
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n353(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION v2_ingest_fund_window(
            BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
        ) TO service_role;
    END IF;
END;
$access$;

ALTER FUNCTION v2_rewind_fund_indexer(
    BIGINT, TEXT, TEXT, BIGINT
) RENAME TO v2_rewind_fund_indexer_b1n340;

CREATE OR REPLACE FUNCTION v2_rewind_fund_indexer(
    p_chain_id BIGINT,
    p_fund_address TEXT,
    p_indexer_name TEXT,
    p_rewind_block BIGINT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
    PERFORM v2_rewind_fund_indexer_b1n340(
        p_chain_id, p_fund_address, p_indexer_name, p_rewind_block
    );
    DELETE FROM v2_fund_strategy_positions
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
END;
$$;

CREATE OR REPLACE FUNCTION v2_replace_fund_deployment(
    p_registry JSONB,
    p_contracts JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    target_chain BIGINT := (p_registry->>'chain_id')::BIGINT;
    target_fund TEXT := p_registry->>'fund_address';
    target_key TEXT := p_registry->>'fund_key';
    strategy TEXT := COALESCE(p_registry->>'strategy_kind', 'csp');
    required_roles TEXT[];
BEGIN
    required_roles := CASE strategy
        WHEN 'covered_call' THEN ARRAY[
            'access_manager', 'address_book', 'batch_settler', 'claim_escrow',
            'controller', 'covered_call_adapter', 'covered_call_valuator',
            'fund_accounting', 'fund_flow_manager', 'fund_share', 'fund_vault',
            'margin_pool', 'nav_verifier', 'oracle', 'otoken_factory',
            'strategy_manager', 'swap_router', 'whitelist'
        ]
        ELSE ARRAY[
            'access_manager', 'address_book', 'batch_settler', 'claim_escrow',
            'controller', 'csp_adapter', 'csp_valuator', 'fund_accounting',
            'fund_flow_manager', 'fund_share', 'fund_vault', 'margin_pool',
            'nav_verifier', 'oracle', 'otoken_factory', 'strategy_manager',
            'swap_router', 'whitelist'
        ]
    END;

    LOCK TABLE v2_fund_registry IN SHARE ROW EXCLUSIVE MODE;
    IF p_registry->>'deployment_status' <> 'DEPLOYED' THEN
        RAISE EXCEPTION 'Deployment handoff must be DEPLOYED';
    END IF;
    IF COALESCE((p_registry->>'handoff_ready')::BOOLEAN, FALSE) IS NOT TRUE THEN
        RAISE EXCEPTION 'Deployment handoff must be fully configured and reconciled';
    END IF;
    IF strategy NOT IN ('csp', 'covered_call') THEN
        RAISE EXCEPTION 'Unsupported fund strategy kind: %', strategy;
    END IF;
    IF jsonb_typeof(p_contracts) <> 'array'
       OR (SELECT array_agg(role ORDER BY role)
           FROM jsonb_array_elements(p_contracts) item,
           LATERAL (SELECT item->>'contract_role' AS role) value)
          IS DISTINCT FROM required_roles THEN
        RAISE EXCEPTION 'Deployment handoff requires the exact trusted role set';
    END IF;
    -- Preserve the B1N-353 same-address reconciliation path: the manifest is
    -- authoritative and its exact role set replaces an incomplete prior handoff.
    DELETE FROM v2_fund_registry
    WHERE chain_id = target_chain AND fund_address = target_fund;
    DELETE FROM v2_fund_contracts
    WHERE chain_id = target_chain AND fund_address = target_fund;
    UPDATE v2_fund_registry
    SET enabled = FALSE, deployment_status = 'RETIRED', updated_at = now()
    WHERE fund_key = target_key;

    INSERT INTO v2_fund_registry (
        chain_id, fund_address, fund_key, start_block, accounting_asset,
        share_token, weth, strategy_kind, quote_asset, enabled,
        deployment_status, share_symbol, share_decimals,
        accounting_asset_symbol, accounting_asset_decimals,
        quote_asset_symbol, quote_asset_decimals
    ) VALUES (
        target_chain, target_fund, target_key,
        (p_registry->>'start_block')::BIGINT,
        p_registry->>'accounting_asset', p_registry->>'share_token',
        p_registry->>'weth', strategy, p_registry->>'quote_asset', FALSE,
        'DEPLOYED', p_registry->>'share_symbol',
        (p_registry->>'share_decimals')::INTEGER,
        p_registry->>'accounting_asset_symbol',
        (p_registry->>'accounting_asset_decimals')::INTEGER,
        p_registry->>'quote_asset_symbol',
        (p_registry->>'quote_asset_decimals')::INTEGER
    );

    INSERT INTO v2_fund_contracts (
        chain_id, fund_address, contract_address, contract_role,
        interface_version, implementation_address, valid_from_block,
        valid_to_block
    )
    SELECT
        target_chain, target_fund, item->>'contract_address',
        item->>'contract_role', (item->>'interface_version')::BIGINT,
        item->>'implementation_address',
        (item->>'valid_from_block')::BIGINT,
        (item->>'valid_to_block')::BIGINT
    FROM jsonb_array_elements(p_contracts) item;

    UPDATE v2_fund_registry
    SET enabled = TRUE, updated_at = now()
    WHERE chain_id = target_chain AND fund_address = target_fund;
END;
$$;

REVOKE ALL ON FUNCTION v2_replace_fund_deployment(JSONB, JSONB) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT ALL ON v2_fund_strategy_positions TO service_role;
        GRANT EXECUTE ON FUNCTION v2_replace_fund_deployment(JSONB, JSONB)
            TO service_role;
    END IF;
END;
$access$;
