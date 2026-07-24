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
    required_roles TEXT[] := ARRAY[
        'access_manager', 'address_book', 'batch_settler', 'claim_escrow',
        'controller', 'csp_adapter', 'csp_valuator', 'fund_accounting',
        'fund_flow_manager', 'fund_share', 'fund_vault', 'margin_pool',
        'nav_verifier', 'oracle', 'otoken_factory', 'strategy_manager',
        'swap_router', 'whitelist'
    ];
BEGIN
    LOCK TABLE v2_fund_registry IN SHARE ROW EXCLUSIVE MODE;
    IF p_registry->>'deployment_status' <> 'DEPLOYED' THEN
        RAISE EXCEPTION 'Deployment handoff must be DEPLOYED';
    END IF;
    IF COALESCE((p_registry->>'handoff_ready')::BOOLEAN, FALSE) IS NOT TRUE THEN
        RAISE EXCEPTION 'Deployment handoff must be fully configured and reconciled';
    END IF;
    IF jsonb_typeof(p_contracts) <> 'array'
       OR (SELECT array_agg(role ORDER BY role)
           FROM jsonb_array_elements(p_contracts) item,
           LATERAL (SELECT item->>'contract_role' AS role) value)
          IS DISTINCT FROM required_roles THEN
        RAISE EXCEPTION 'Deployment handoff requires the exact trusted role set';
    END IF;

    -- Same-address reconciliation is allowed only as an explicit replacement:
    -- retire the existing row and its bindings, then insert the canonical v2 set.
    DELETE FROM v2_fund_registry
    WHERE chain_id = target_chain AND fund_address = target_fund;
    DELETE FROM v2_fund_contracts
    WHERE chain_id = target_chain AND fund_address = target_fund;
    UPDATE v2_fund_registry
    SET enabled = FALSE, deployment_status = 'RETIRED', updated_at = now()
    WHERE fund_key = target_key;

    INSERT INTO v2_fund_registry (
        chain_id, fund_address, fund_key, start_block, accounting_asset,
        share_token, weth, enabled, deployment_status, share_symbol,
        share_decimals, accounting_asset_symbol, accounting_asset_decimals
    ) VALUES (
        target_chain, target_fund, target_key,
        (p_registry->>'start_block')::BIGINT,
        p_registry->>'accounting_asset', p_registry->>'share_token',
        p_registry->>'weth', FALSE, 'DEPLOYED', p_registry->>'share_symbol',
        (p_registry->>'share_decimals')::INTEGER,
        p_registry->>'accounting_asset_symbol',
        (p_registry->>'accounting_asset_decimals')::INTEGER
    );

    INSERT INTO v2_fund_contracts (
        chain_id, fund_address, contract_address, contract_role,
        interface_version, implementation_address, valid_from_block,
        valid_to_block
    )
    SELECT target_chain, target_fund, item->>'contract_address',
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
