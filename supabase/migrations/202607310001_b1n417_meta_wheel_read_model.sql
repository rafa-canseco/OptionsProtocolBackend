-- B1N-417: isolated Meta Wheel projection and coherent parent NAV read model.

ALTER TABLE v2_fund_registry
    DROP CONSTRAINT IF EXISTS v2_fund_registry_strategy_kind_check,
    DROP CONSTRAINT IF EXISTS v2_fund_registry_quote_asset_check;

ALTER TABLE v2_fund_registry
    ADD CONSTRAINT v2_fund_registry_strategy_kind_check
        CHECK (strategy_kind IN ('csp', 'covered_call', 'meta_wheel')),
    ADD CONSTRAINT v2_fund_registry_quote_asset_check CHECK (
        (
            strategy_kind IN ('csp', 'meta_wheel')
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

ALTER TABLE v2_fund_state
    DROP CONSTRAINT IF EXISTS v2_fund_state_strategy_kind_check;
ALTER TABLE v2_fund_state
    ADD CONSTRAINT v2_fund_state_strategy_kind_check
        CHECK (strategy_kind IN ('csp', 'covered_call', 'meta_wheel'));

ALTER TABLE v2_fund_contracts
    DROP CONSTRAINT IF EXISTS v2_fund_contracts_contract_role_check,
    DROP CONSTRAINT IF EXISTS v2_fund_contracts_proxy_implementation_check;

ALTER TABLE v2_fund_contracts
    ADD CONSTRAINT v2_fund_contracts_contract_role_check CHECK (
        contract_role IN (
            'fund_vault', 'fund_share', 'fund_accounting',
            'fund_flow_manager', 'strategy_manager', 'csp_adapter',
            'covered_call_adapter', 'wheel_coordinator', 'controller',
            'batch_settler', 'claim_escrow', 'access_manager',
            'address_book', 'csp_valuator', 'covered_call_valuator',
            'meta_wheel_valuator', 'margin_pool', 'nav_verifier', 'oracle',
            'otoken_factory', 'swap_router', 'whitelist'
        )
    ),
    ADD CONSTRAINT v2_fund_contracts_proxy_implementation_check CHECK (
        contract_role NOT IN (
            'fund_vault', 'fund_share', 'fund_accounting',
            'fund_flow_manager', 'strategy_manager', 'csp_adapter',
            'covered_call_adapter', 'wheel_coordinator', 'controller',
            'batch_settler'
        )
        OR implementation_address IS NOT NULL
    );

CREATE TABLE v2_meta_wheel_state (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL CHECK (fund_address = lower(fund_address)),
    pending_csp_usdc NUMERIC(78, 0) NOT NULL DEFAULT 0,
    redemption_reserved_usdc NUMERIC(78, 0) NOT NULL DEFAULT 0,
    reserved_principal_usdc NUMERIC(78, 0) NOT NULL DEFAULT 0,
    transition_weth NUMERIC(78, 0) NOT NULL DEFAULT 0,
    policy_version BIGINT NOT NULL DEFAULT 0,
    policy_hash TEXT,
    execution_cost_buffer_8 NUMERIC(78, 0) NOT NULL DEFAULT 0,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    cumulative_gross_premium NUMERIC(78, 0) NOT NULL DEFAULT 0,
    cumulative_protocol_fee NUMERIC(78, 0) NOT NULL DEFAULT 0,
    cumulative_net_premium NUMERIC(78, 0) NOT NULL DEFAULT 0,
    upgrade_count BIGINT NOT NULL DEFAULT 0,
    last_upgrade_implementation TEXT,
    current_phase TEXT NOT NULL,
    next_action TEXT NOT NULL,
    active_tranche_count INTEGER NOT NULL DEFAULT 0,
    protected_assignment_floor_8 NUMERIC(78, 0) NOT NULL DEFAULT 0,
    last_event_block BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, fund_address),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE,
    CHECK (
        cumulative_gross_premium =
            cumulative_protocol_fee + cumulative_net_premium
    )
);

CREATE TABLE v2_meta_wheel_lanes (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    child_vault TEXT NOT NULL CHECK (child_vault = lower(child_vault)),
    registration_index INTEGER NOT NULL CHECK (registration_index >= 0),
    lane_type TEXT NOT NULL CHECK (lane_type IN ('csp', 'covered_call')),
    active BOOLEAN NOT NULL,
    last_event_block BIGINT NOT NULL,
    PRIMARY KEY (chain_id, fund_address, child_vault),
    UNIQUE (chain_id, fund_address, registration_index),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_meta_wheel_state (chain_id, fund_address)
        ON DELETE CASCADE
);

CREATE TABLE v2_meta_wheel_tranches (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    tranche_id NUMERIC(78, 0) NOT NULL,
    allocation_id TEXT,
    parent_tranche_id NUMERIC(78, 0),
    child_vault TEXT CHECK (child_vault = lower(child_vault)),
    state TEXT NOT NULL CHECK (state IN (
        'pending_csp', 'csp_open', 'csp_settling', 'weth_transition',
        'call_open', 'call_settling', 'closed'
    )),
    principal_assets NUMERIC(78, 0) NOT NULL,
    pending_assets NUMERIC(78, 0) NOT NULL DEFAULT 0,
    state_nonce BIGINT NOT NULL,
    state_hash TEXT,
    expiry BIGINT,
    child_position_id NUMERIC(78, 0),
    child_execution_state_hash TEXT,
    settlement_kind TEXT CHECK (settlement_kind IN (
        'pending_delivery', 'csp_otm', 'csp_assigned', 'call_otm',
        'call_away', 'weth_fallback'
    )),
    child_shares NUMERIC(78, 0) NOT NULL DEFAULT 0,
    assignment_lot_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    literal_call_floor_8 NUMERIC(78, 0) NOT NULL DEFAULT 0,
    required_call_floor_8 NUMERIC(78, 0) NOT NULL DEFAULT 0,
    call_strike_8 NUMERIC(78, 0),
    last_event_block BIGINT NOT NULL,
    PRIMARY KEY (chain_id, fund_address, tranche_id),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_meta_wheel_state (chain_id, fund_address)
        ON DELETE CASCADE,
    FOREIGN KEY (chain_id, fund_address, child_vault)
        REFERENCES v2_meta_wheel_lanes (chain_id, fund_address, child_vault),
    FOREIGN KEY (chain_id, fund_address, parent_tranche_id)
        REFERENCES v2_meta_wheel_tranches (chain_id, fund_address, tranche_id)
);

CREATE INDEX v2_meta_wheel_tranches_active_idx
    ON v2_meta_wheel_tranches (chain_id, fund_address, state, tranche_id)
    WHERE state <> 'closed';

CREATE TABLE v2_meta_wheel_assignment_lots (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    lot_id NUMERIC(78, 0) NOT NULL,
    origin_tranche_id NUMERIC(78, 0) NOT NULL,
    origin_csp_child_vault TEXT NOT NULL CHECK (
        origin_csp_child_vault = lower(origin_csp_child_vault)
    ),
    origin_csp_position_id NUMERIC(78, 0) NOT NULL,
    weth_received NUMERIC(78, 0) NOT NULL CHECK (weth_received > 0),
    remaining_weth NUMERIC(78, 0) NOT NULL,
    literal_assignment_strike_8 NUMERIC(78, 0) NOT NULL CHECK (
        literal_assignment_strike_8 > 0
    ),
    status TEXT NOT NULL CHECK (
        status IN ('available', 'in_call', 'called_away', 'emergency_exited')
    ),
    created_block BIGINT NOT NULL,
    last_event_block BIGINT NOT NULL,
    PRIMARY KEY (chain_id, fund_address, lot_id),
    FOREIGN KEY (chain_id, fund_address, origin_tranche_id)
        REFERENCES v2_meta_wheel_tranches (
            chain_id, fund_address, tranche_id
        ) ON DELETE CASCADE,
    CHECK (remaining_weth BETWEEN 0 AND weth_received)
);

CREATE TABLE v2_meta_wheel_handoffs (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    handoff_id TEXT NOT NULL,
    tranche_id NUMERIC(78, 0) NOT NULL,
    child_vault TEXT NOT NULL CHECK (child_vault = lower(child_vault)),
    direction TEXT NOT NULL CHECK (
        direction IN ('csp_to_csp', 'csp_to_call', 'call_to_call', 'call_to_csp')
    ),
    settlement_kind TEXT NOT NULL CHECK (settlement_kind IN (
        'csp_otm', 'csp_assigned', 'call_otm', 'call_away', 'weth_fallback'
    )),
    transition_nonce BIGINT NOT NULL,
    usdc_amount NUMERIC(78, 0) NOT NULL,
    weth_amount NUMERIC(78, 0) NOT NULL,
    child_shares_burned NUMERIC(78, 0) NOT NULL,
    block_number BIGINT NOT NULL,
    transaction_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    PRIMARY KEY (chain_id, fund_address, handoff_id),
    UNIQUE (chain_id, fund_address, tranche_id, transition_nonce, direction),
    FOREIGN KEY (chain_id, fund_address, tranche_id)
        REFERENCES v2_meta_wheel_tranches (
            chain_id, fund_address, tranche_id
        ) ON DELETE CASCADE
);

-- Fresh, block-bound reports produced by the dedicated child valuation workers.
-- These rows are the only payload accepted by the parent NAV reporter.
CREATE TABLE v2_meta_wheel_lane_valuations (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    child_vault TEXT NOT NULL CHECK (child_vault = lower(child_vault)),
    strategy_kind TEXT NOT NULL CHECK (strategy_kind IN ('csp', 'covered_call')),
    custody_domain TEXT NOT NULL CHECK (custody_domain = lower(custody_domain)),
    snapshot_block BIGINT NOT NULL,
    snapshot_block_hash TEXT NOT NULL,
    valid_after_block BIGINT NOT NULL,
    valid_until_block BIGINT NOT NULL,
    child_shares NUMERIC(78, 0) NOT NULL CHECK (child_shares > 0),
    gross_assets_usdc NUMERIC(78, 0) NOT NULL,
    liabilities_usdc NUMERIC(78, 0) NOT NULL,
    liquid_usdc NUMERIC(78, 0) NOT NULL,
    base_exit_cost_usdc NUMERIC(78, 0) NOT NULL,
    position_state_hash TEXT NOT NULL,
    data_hash TEXT NOT NULL,
    valuation_data TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain_id, fund_address, child_vault, snapshot_block),
    UNIQUE (chain_id, fund_address, custody_domain, snapshot_block),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE,
    CHECK (valid_after_block <= snapshot_block),
    CHECK (snapshot_block <= valid_until_block),
    CHECK (liabilities_usdc <= gross_assets_usdc),
    CHECK (liquid_usdc <= gross_assets_usdc)
);

CREATE TABLE v2_meta_wheel_nav_snapshots (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    report_nonce BIGINT NOT NULL,
    snapshot_block BIGINT NOT NULL,
    snapshot_block_hash TEXT NOT NULL,
    coherent BOOLEAN NOT NULL CHECK (coherent),
    gross_assets NUMERIC(78, 0) NOT NULL,
    liabilities NUMERIC(78, 0) NOT NULL,
    net_assets NUMERIC(78, 0) NOT NULL,
    parent_idle_usdc NUMERIC(78, 0) NOT NULL,
    pending_csp_usdc NUMERIC(78, 0) NOT NULL,
    redemption_reserved_usdc NUMERIC(78, 0) NOT NULL,
    transition_weth NUMERIC(78, 0) NOT NULL,
    transition_weth_value_assets NUMERIC(78, 0) NOT NULL,
    child_csp_value_assets NUMERIC(78, 0) NOT NULL,
    child_covered_call_value_assets NUMERIC(78, 0) NOT NULL,
    parent_exit_cost_usdc NUMERIC(78, 0) NOT NULL,
    weth_spot_price_8 NUMERIC(78, 0) NOT NULL,
    stress_net_assets NUMERIC(78, 0),
    child_reports JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain_id, fund_address, report_nonce),
    UNIQUE (chain_id, fund_address, snapshot_block, snapshot_block_hash),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address)
        ON DELETE CASCADE,
    CHECK (net_assets = gross_assets - liabilities)
);

ALTER TABLE v2_meta_wheel_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_meta_wheel_lanes ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_meta_wheel_tranches ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_meta_wheel_assignment_lots ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_meta_wheel_handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_meta_wheel_lane_valuations ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_meta_wheel_nav_snapshots ENABLE ROW LEVEL SECURITY;

ALTER FUNCTION v2_ingest_fund_window(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) RENAME TO v2_ingest_fund_window_b1n417;

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
    strategy TEXT;
BEGIN
    PERFORM v2_ingest_fund_window_b1n417(
        p_chain_id, p_fund_address, p_indexer_name, p_from_block,
        p_to_block, p_last_block_hash, p_events, p_projection
    );

    SELECT strategy_kind INTO strategy
    FROM v2_fund_registry
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    IF strategy IS DISTINCT FROM 'meta_wheel' THEN
        RETURN;
    END IF;

    DELETE FROM v2_meta_wheel_handoffs
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_meta_wheel_assignment_lots
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_meta_wheel_tranches
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_meta_wheel_lanes
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_meta_wheel_state
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;

    INSERT INTO v2_meta_wheel_state (
        chain_id, fund_address, pending_csp_usdc, redemption_reserved_usdc,
        reserved_principal_usdc, transition_weth, policy_version, policy_hash,
        execution_cost_buffer_8, paused,
        cumulative_gross_premium, cumulative_protocol_fee,
        cumulative_net_premium, upgrade_count,
        last_upgrade_implementation, current_phase,
        next_action, active_tranche_count, protected_assignment_floor_8,
        last_event_block
    )
    SELECT * FROM jsonb_to_recordset(
        COALESCE(p_projection->'wheel_state', '[]'::jsonb)
    ) AS x(
        chain_id BIGINT, fund_address TEXT, pending_csp_usdc NUMERIC,
        redemption_reserved_usdc NUMERIC, reserved_principal_usdc NUMERIC,
        transition_weth NUMERIC,
        policy_version BIGINT,
        policy_hash TEXT, execution_cost_buffer_8 NUMERIC,
        paused BOOLEAN,
        cumulative_gross_premium NUMERIC, cumulative_protocol_fee NUMERIC,
        cumulative_net_premium NUMERIC, upgrade_count BIGINT,
        last_upgrade_implementation TEXT,
        current_phase TEXT, next_action TEXT, active_tranche_count INTEGER,
        protected_assignment_floor_8 NUMERIC, last_event_block BIGINT
    );

    INSERT INTO v2_meta_wheel_lanes
    SELECT * FROM jsonb_to_recordset(
        COALESCE(p_projection->'wheel_lanes', '[]'::jsonb)
    ) AS x(
        chain_id BIGINT, fund_address TEXT, child_vault TEXT,
        registration_index INTEGER, lane_type TEXT, active BOOLEAN,
        last_event_block BIGINT
    );

    INSERT INTO v2_meta_wheel_tranches
    SELECT * FROM jsonb_to_recordset(
        COALESCE(p_projection->'wheel_tranches', '[]'::jsonb)
    ) AS x(
        chain_id BIGINT, fund_address TEXT, tranche_id NUMERIC,
        allocation_id TEXT, parent_tranche_id NUMERIC,
        child_vault TEXT, state TEXT,
        principal_assets NUMERIC, pending_assets NUMERIC,
        state_nonce BIGINT, state_hash TEXT, expiry BIGINT,
        child_position_id NUMERIC, child_execution_state_hash TEXT,
        settlement_kind TEXT,
        child_shares NUMERIC, assignment_lot_ids JSONB,
        literal_call_floor_8 NUMERIC,
        required_call_floor_8 NUMERIC, call_strike_8 NUMERIC,
        last_event_block BIGINT
    );

    INSERT INTO v2_meta_wheel_assignment_lots
    SELECT * FROM jsonb_to_recordset(
        COALESCE(p_projection->'wheel_assignment_lots', '[]'::jsonb)
    ) AS x(
        chain_id BIGINT, fund_address TEXT, lot_id NUMERIC,
        origin_tranche_id NUMERIC, origin_csp_child_vault TEXT,
        origin_csp_position_id NUMERIC, weth_received NUMERIC,
        remaining_weth NUMERIC, literal_assignment_strike_8 NUMERIC,
        status TEXT, created_block BIGINT, last_event_block BIGINT
    );

    INSERT INTO v2_meta_wheel_handoffs
    SELECT * FROM jsonb_to_recordset(
        COALESCE(p_projection->'wheel_handoffs', '[]'::jsonb)
    ) AS x(
        chain_id BIGINT, fund_address TEXT, handoff_id TEXT,
        tranche_id NUMERIC, child_vault TEXT, direction TEXT,
        settlement_kind TEXT, transition_nonce BIGINT,
        usdc_amount NUMERIC, weth_amount NUMERIC,
        child_shares_burned NUMERIC, block_number BIGINT,
        transaction_hash TEXT, log_index INTEGER
    );
END;
$$;

ALTER FUNCTION v2_rewind_fund_indexer(
    BIGINT, TEXT, TEXT, BIGINT
) RENAME TO v2_rewind_fund_indexer_b1n417;

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
    PERFORM v2_rewind_fund_indexer_b1n417(
        p_chain_id, p_fund_address, p_indexer_name, p_rewind_block
    );
    DELETE FROM v2_meta_wheel_state
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_meta_wheel_nav_snapshots
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address
      AND snapshot_block >= p_rewind_block;
    DELETE FROM v2_meta_wheel_lane_valuations
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address
      AND snapshot_block >= p_rewind_block;
END;
$$;

CREATE OR REPLACE FUNCTION v2_upsert_meta_wheel_nav_snapshot(
    p_snapshot JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    target_chain BIGINT := (p_snapshot->>'chain_id')::BIGINT;
    target_fund TEXT := p_snapshot->>'fund_address';
    target_nonce BIGINT := (p_snapshot->>'report_nonce')::BIGINT;
    existing_block BIGINT;
    existing_hash TEXT;
BEGIN
    IF NOT COALESCE((p_snapshot->>'coherent')::BOOLEAN, FALSE) THEN
        RAISE EXCEPTION 'Meta Wheel NAV snapshot must be coherent';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM v2_fund_registry
        WHERE chain_id = target_chain AND fund_address = target_fund
          AND enabled AND strategy_kind = 'meta_wheel'
    ) THEN
        RAISE EXCEPTION 'Enabled Meta Wheel registry does not exist';
    END IF;
    SELECT snapshot_block, snapshot_block_hash
    INTO existing_block, existing_hash
    FROM v2_meta_wheel_nav_snapshots
    WHERE chain_id = target_chain AND fund_address = target_fund
      AND report_nonce = target_nonce
    FOR UPDATE;
    IF existing_block IS NOT NULL AND (
        existing_block <> (p_snapshot->>'snapshot_block')::BIGINT
        OR existing_hash IS DISTINCT FROM p_snapshot->>'snapshot_block_hash'
    ) THEN
        RAISE EXCEPTION 'Meta Wheel NAV nonce is already bound to another block';
    END IF;

    INSERT INTO v2_meta_wheel_nav_snapshots (
        chain_id, fund_address, report_nonce, snapshot_block,
        snapshot_block_hash, coherent, gross_assets, liabilities, net_assets,
        parent_idle_usdc, pending_csp_usdc, redemption_reserved_usdc,
        transition_weth,
        transition_weth_value_assets, child_csp_value_assets,
        child_covered_call_value_assets, parent_exit_cost_usdc,
        weth_spot_price_8, stress_net_assets, child_reports, observed_at
    ) VALUES (
        target_chain, target_fund, target_nonce,
        (p_snapshot->>'snapshot_block')::BIGINT,
        p_snapshot->>'snapshot_block_hash', TRUE,
        (p_snapshot->>'gross_assets')::NUMERIC,
        (p_snapshot->>'liabilities')::NUMERIC,
        (p_snapshot->>'net_assets')::NUMERIC,
        (p_snapshot->>'parent_idle_usdc')::NUMERIC,
        (p_snapshot->>'pending_csp_usdc')::NUMERIC,
        (p_snapshot->>'redemption_reserved_usdc')::NUMERIC,
        (p_snapshot->>'transition_weth')::NUMERIC,
        (p_snapshot->>'transition_weth_value_assets')::NUMERIC,
        (p_snapshot->>'child_csp_value_assets')::NUMERIC,
        (p_snapshot->>'child_covered_call_value_assets')::NUMERIC,
        (p_snapshot->>'parent_exit_cost_usdc')::NUMERIC,
        (p_snapshot->>'weth_spot_price_8')::NUMERIC,
        (p_snapshot->>'stress_net_assets')::NUMERIC,
        COALESCE(p_snapshot->'child_reports', '[]'::jsonb),
        (p_snapshot->>'observed_at')::TIMESTAMPTZ
    )
    ON CONFLICT (chain_id, fund_address, report_nonce) DO UPDATE SET
        coherent = EXCLUDED.coherent,
        gross_assets = EXCLUDED.gross_assets,
        liabilities = EXCLUDED.liabilities,
        net_assets = EXCLUDED.net_assets,
        parent_idle_usdc = EXCLUDED.parent_idle_usdc,
        pending_csp_usdc = EXCLUDED.pending_csp_usdc,
        redemption_reserved_usdc = EXCLUDED.redemption_reserved_usdc,
        transition_weth = EXCLUDED.transition_weth,
        transition_weth_value_assets = EXCLUDED.transition_weth_value_assets,
        child_csp_value_assets = EXCLUDED.child_csp_value_assets,
        child_covered_call_value_assets = EXCLUDED.child_covered_call_value_assets,
        parent_exit_cost_usdc = EXCLUDED.parent_exit_cost_usdc,
        weth_spot_price_8 = EXCLUDED.weth_spot_price_8,
        stress_net_assets = EXCLUDED.stress_net_assets,
        child_reports = EXCLUDED.child_reports,
        observed_at = EXCLUDED.observed_at;
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
        WHEN 'meta_wheel' THEN ARRAY[
            'access_manager', 'address_book', 'batch_settler', 'claim_escrow',
            'controller', 'fund_accounting', 'fund_flow_manager', 'fund_share',
            'fund_vault', 'margin_pool', 'meta_wheel_valuator', 'nav_verifier',
            'oracle', 'otoken_factory', 'strategy_manager', 'swap_router',
            'wheel_coordinator', 'whitelist'
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
    IF p_registry->>'deployment_status' <> 'DEPLOYED'
       OR COALESCE((p_registry->>'handoff_ready')::BOOLEAN, FALSE) IS NOT TRUE THEN
        RAISE EXCEPTION 'Deployment handoff must be DEPLOYED and reconciled';
    END IF;
    IF strategy NOT IN ('csp', 'covered_call', 'meta_wheel') THEN
        RAISE EXCEPTION 'Unsupported fund strategy kind: %', strategy;
    END IF;
    IF jsonb_typeof(p_contracts) <> 'array'
       OR (SELECT array_agg(role ORDER BY role)
           FROM jsonb_array_elements(p_contracts) item,
           LATERAL (SELECT item->>'contract_role' AS role) value)
          IS DISTINCT FROM required_roles THEN
        RAISE EXCEPTION 'Deployment handoff requires the exact trusted role set';
    END IF;

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

REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n417(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_upsert_meta_wheel_nav_snapshot(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_replace_fund_deployment(JSONB, JSONB) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT ALL ON v2_meta_wheel_state TO service_role;
        GRANT ALL ON v2_meta_wheel_lanes TO service_role;
        GRANT ALL ON v2_meta_wheel_tranches TO service_role;
        GRANT ALL ON v2_meta_wheel_assignment_lots TO service_role;
        GRANT ALL ON v2_meta_wheel_handoffs TO service_role;
        GRANT ALL ON v2_meta_wheel_lane_valuations TO service_role;
        GRANT ALL ON v2_meta_wheel_nav_snapshots TO service_role;
        GRANT EXECUTE ON FUNCTION v2_ingest_fund_window(
            BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v2_upsert_meta_wheel_nav_snapshot(JSONB)
            TO service_role;
        GRANT EXECUTE ON FUNCTION v2_replace_fund_deployment(JSONB, JSONB)
            TO service_role;
    END IF;
END;
$access$;
