-- B1N-417: immutable-at-block persistence for executable Wheel NAV.

CREATE OR REPLACE FUNCTION v2_store_meta_wheel_lane_valuation(
    p_valuation JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    target_chain BIGINT := (p_valuation->>'chain_id')::BIGINT;
    target_fund TEXT := lower(p_valuation->>'fund_address');
    target_lane TEXT := lower(p_valuation->>'child_vault');
    target_block BIGINT := (p_valuation->>'snapshot_block')::BIGINT;
    existing v2_meta_wheel_lane_valuations%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        target_chain::TEXT || ':' || target_fund || ':' || target_lane
            || ':' || target_block::TEXT, 0
    ));
    IF NOT EXISTS (
        SELECT 1 FROM v2_fund_registry
        WHERE chain_id = target_chain AND fund_address = target_fund
          AND enabled AND strategy_kind = 'meta_wheel'
    ) THEN
        RAISE EXCEPTION 'Enabled Meta Wheel registry does not exist';
    END IF;

    SELECT * INTO existing
    FROM v2_meta_wheel_lane_valuations
    WHERE chain_id = target_chain AND fund_address = target_fund
      AND child_vault = target_lane AND snapshot_block = target_block;
    IF FOUND THEN
        IF existing.snapshot_block_hash IS DISTINCT FROM lower(p_valuation->>'snapshot_block_hash')
           OR existing.strategy_kind IS DISTINCT FROM p_valuation->>'strategy_kind'
           OR existing.custody_domain IS DISTINCT FROM lower(p_valuation->>'custody_domain')
           OR existing.valid_after_block IS DISTINCT FROM (p_valuation->>'valid_after_block')::BIGINT
           OR existing.valid_until_block IS DISTINCT FROM (p_valuation->>'valid_until_block')::BIGINT
           OR existing.child_shares IS DISTINCT FROM (p_valuation->>'child_shares')::NUMERIC
           OR existing.gross_assets_usdc IS DISTINCT FROM (p_valuation->>'gross_assets_usdc')::NUMERIC
           OR existing.liabilities_usdc IS DISTINCT FROM (p_valuation->>'liabilities_usdc')::NUMERIC
           OR existing.liquid_usdc IS DISTINCT FROM (p_valuation->>'liquid_usdc')::NUMERIC
           OR existing.base_exit_cost_usdc IS DISTINCT FROM (p_valuation->>'base_exit_cost_usdc')::NUMERIC
           OR existing.position_state_hash IS DISTINCT FROM lower(p_valuation->>'position_state_hash')
           OR existing.data_hash IS DISTINCT FROM lower(p_valuation->>'data_hash')
           OR existing.valuation_data IS DISTINCT FROM lower(p_valuation->>'valuation_data')
        THEN
            RAISE EXCEPTION 'Meta Wheel lane valuation is already bound differently';
        END IF;
        RETURN;
    END IF;

    INSERT INTO v2_meta_wheel_lane_valuations (
        chain_id, fund_address, child_vault, strategy_kind, custody_domain,
        snapshot_block, snapshot_block_hash, valid_after_block,
        valid_until_block, child_shares, gross_assets_usdc,
        liabilities_usdc, liquid_usdc, base_exit_cost_usdc,
        position_state_hash, data_hash, valuation_data, observed_at
    ) VALUES (
        target_chain, target_fund, target_lane,
        p_valuation->>'strategy_kind', lower(p_valuation->>'custody_domain'),
        target_block, lower(p_valuation->>'snapshot_block_hash'),
        (p_valuation->>'valid_after_block')::BIGINT,
        (p_valuation->>'valid_until_block')::BIGINT,
        (p_valuation->>'child_shares')::NUMERIC,
        (p_valuation->>'gross_assets_usdc')::NUMERIC,
        (p_valuation->>'liabilities_usdc')::NUMERIC,
        (p_valuation->>'liquid_usdc')::NUMERIC,
        (p_valuation->>'base_exit_cost_usdc')::NUMERIC,
        lower(p_valuation->>'position_state_hash'),
        lower(p_valuation->>'data_hash'), lower(p_valuation->>'valuation_data'),
        (p_valuation->>'observed_at')::TIMESTAMPTZ
    );
END;
$$;

CREATE OR REPLACE FUNCTION v2_store_meta_wheel_nav_snapshot(
    p_snapshot JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    target_chain BIGINT := (p_snapshot->>'chain_id')::BIGINT;
    target_fund TEXT := lower(p_snapshot->>'fund_address');
    target_nonce BIGINT := (p_snapshot->>'report_nonce')::BIGINT;
    existing v2_meta_wheel_nav_snapshots%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        target_chain::TEXT || ':' || target_fund || ':' || target_nonce::TEXT, 0
    ));
    SELECT * INTO existing
    FROM v2_meta_wheel_nav_snapshots
    WHERE chain_id = target_chain AND fund_address = target_fund
      AND report_nonce = target_nonce;
    IF FOUND THEN
        IF existing.snapshot_block IS DISTINCT FROM (p_snapshot->>'snapshot_block')::BIGINT
           OR existing.snapshot_block_hash IS DISTINCT FROM lower(p_snapshot->>'snapshot_block_hash')
           OR existing.coherent IS DISTINCT FROM TRUE
           OR existing.gross_assets IS DISTINCT FROM (p_snapshot->>'gross_assets')::NUMERIC
           OR existing.liabilities IS DISTINCT FROM (p_snapshot->>'liabilities')::NUMERIC
           OR existing.net_assets IS DISTINCT FROM (p_snapshot->>'net_assets')::NUMERIC
           OR existing.parent_idle_usdc IS DISTINCT FROM (p_snapshot->>'parent_idle_usdc')::NUMERIC
           OR existing.pending_csp_usdc IS DISTINCT FROM (p_snapshot->>'pending_csp_usdc')::NUMERIC
           OR existing.redemption_reserved_usdc IS DISTINCT FROM (p_snapshot->>'redemption_reserved_usdc')::NUMERIC
           OR existing.transition_weth IS DISTINCT FROM (p_snapshot->>'transition_weth')::NUMERIC
           OR existing.transition_weth_value_assets IS DISTINCT FROM (p_snapshot->>'transition_weth_value_assets')::NUMERIC
           OR existing.child_csp_value_assets IS DISTINCT FROM (p_snapshot->>'child_csp_value_assets')::NUMERIC
           OR existing.child_covered_call_value_assets IS DISTINCT FROM (p_snapshot->>'child_covered_call_value_assets')::NUMERIC
           OR existing.parent_exit_cost_usdc IS DISTINCT FROM (p_snapshot->>'parent_exit_cost_usdc')::NUMERIC
           OR existing.weth_spot_price_8 IS DISTINCT FROM (p_snapshot->>'weth_spot_price_8')::NUMERIC
           OR existing.stress_net_assets IS DISTINCT FROM (p_snapshot->>'stress_net_assets')::NUMERIC
           OR existing.child_reports IS DISTINCT FROM COALESCE(p_snapshot->'child_reports', '[]'::JSONB)
        THEN
            RAISE EXCEPTION 'Meta Wheel NAV snapshot is already bound differently';
        END IF;
        RETURN;
    END IF;
    PERFORM v2_upsert_meta_wheel_nav_snapshot(p_snapshot);
END;
$$;

DO $$
BEGIN
    REVOKE ALL ON FUNCTION v2_store_meta_wheel_lane_valuation(JSONB)
        FROM PUBLIC;
    REVOKE ALL ON FUNCTION v2_store_meta_wheel_nav_snapshot(JSONB)
        FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION v2_store_meta_wheel_lane_valuation(JSONB)
            TO service_role;
        GRANT EXECUTE ON FUNCTION v2_store_meta_wheel_nav_snapshot(JSONB)
            TO service_role;
    END IF;
END;
$$;
