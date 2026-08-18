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
    target_fund TEXT := lower(p_registry->>'fund_address');
    target_key TEXT := p_registry->>'fund_key';
    strategy TEXT := COALESCE(p_registry->>'strategy_kind', 'csp');
    required_roles TEXT[];
    existing_key TEXT;
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
       OR (SELECT array_agg(DISTINCT role ORDER BY role)
           FROM jsonb_array_elements(p_contracts) item,
           LATERAL (SELECT item->>'contract_role' AS role) value)
          IS DISTINCT FROM required_roles
       OR EXISTS (
           SELECT 1
           FROM jsonb_array_elements(p_contracts) item
           GROUP BY item->>'contract_role'
           HAVING count(*) FILTER (
               WHERE item->>'valid_to_block' IS NULL
           ) <> 1
       ) THEN
        RAISE EXCEPTION
            'Deployment handoff requires exact roles and one active binding per role';
    END IF;

    SELECT fund_key
    INTO existing_key
    FROM v2_fund_registry
    WHERE chain_id = target_chain AND fund_address = target_fund
    FOR UPDATE;

    IF FOUND THEN
        IF existing_key <> target_key THEN
            RAISE EXCEPTION 'Registered fund address belongs to a different fund key';
        END IF;
        UPDATE v2_fund_registry
        SET
            start_block = (p_registry->>'start_block')::BIGINT,
            accounting_asset = lower(p_registry->>'accounting_asset'),
            share_token = lower(p_registry->>'share_token'),
            weth = lower(p_registry->>'weth'),
            strategy_kind = strategy,
            quote_asset = lower(p_registry->>'quote_asset'),
            enabled = FALSE,
            deployment_status = 'DEPLOYED',
            share_symbol = p_registry->>'share_symbol',
            share_decimals = (p_registry->>'share_decimals')::INTEGER,
            accounting_asset_symbol = p_registry->>'accounting_asset_symbol',
            accounting_asset_decimals =
                (p_registry->>'accounting_asset_decimals')::INTEGER,
            quote_asset_symbol = p_registry->>'quote_asset_symbol',
            quote_asset_decimals =
                (p_registry->>'quote_asset_decimals')::INTEGER,
            updated_at = now()
        WHERE chain_id = target_chain AND fund_address = target_fund;
        DELETE FROM v2_fund_contracts
        WHERE chain_id = target_chain AND fund_address = target_fund;
    ELSE
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
            lower(p_registry->>'accounting_asset'),
            lower(p_registry->>'share_token'),
            lower(p_registry->>'weth'), strategy,
            lower(p_registry->>'quote_asset'), FALSE,
            'DEPLOYED', p_registry->>'share_symbol',
            (p_registry->>'share_decimals')::INTEGER,
            p_registry->>'accounting_asset_symbol',
            (p_registry->>'accounting_asset_decimals')::INTEGER,
            p_registry->>'quote_asset_symbol',
            (p_registry->>'quote_asset_decimals')::INTEGER
        );
    END IF;

    INSERT INTO v2_fund_contracts (
        chain_id, fund_address, contract_address, contract_role,
        interface_version, implementation_address, valid_from_block,
        valid_to_block
    )
    SELECT
        target_chain, target_fund, lower(item->>'contract_address'),
        item->>'contract_role', (item->>'interface_version')::BIGINT,
        NULLIF(lower(item->>'implementation_address'), ''),
        (item->>'valid_from_block')::BIGINT,
        (item->>'valid_to_block')::BIGINT
    FROM jsonb_array_elements(p_contracts) item;

    UPDATE v2_fund_registry
    SET enabled = TRUE, updated_at = now()
    WHERE chain_id = target_chain AND fund_address = target_fund;
END;
$$;

REVOKE ALL ON FUNCTION v2_replace_fund_deployment(JSONB, JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION v2_replace_fund_deployment(JSONB, JSONB)
    TO service_role;
