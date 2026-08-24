INSERT INTO mm_api_keys (mm_address, api_key, is_active)
VALUES ('0x1111111111111111111111111111111111111111', 'integration-key', TRUE);

INSERT INTO v2_fund_registry (
    chain_id, fund_address, fund_key, start_block, accounting_asset,
    share_token, weth, enabled, strategy_kind, quote_asset,
    quote_asset_symbol, quote_asset_decimals, accounting_role_account,
    deployment_status, accounting_asset_symbol, accounting_asset_decimals,
    share_symbol, share_decimals
) VALUES
(84532, '0x4444444444444444444444444444444444444444', 'eth-usdc-csp', 1,
 '0x2222222222222222222222222222222222222222', '0x4444444444444444444444444444444444444445',
 '0x5555555555555555555555555555555555555555', TRUE, 'csp', NULL, NULL, NULL, NULL,
 'DEPLOYED', 'USDC', 6, 'b1CSP', 18),
(84532, '0x9999999999999999999999999999999999999999', 'eth-weth-covered-call', 1,
 '0x5555555555555555555555555555555555555555', '0x9999999999999999999999999999999999999998',
 '0x5555555555555555555555555555555555555555', TRUE, 'covered_call',
 '0x2222222222222222222222222222222222222222', 'USDC', 6, NULL,
 'DEPLOYED', 'WETH', 18, 'b1CALL', 18),
(84532, '0xcccccccccccccccccccccccccccccccccccccccc', 'eth-usdc-meta-wheel', 1,
 '0x2222222222222222222222222222222222222222', '0xcccccccccccccccccccccccccccccccccccccccd',
 '0x5555555555555555555555555555555555555555', TRUE, 'meta_wheel', NULL, NULL, NULL,
 '0xccccccccccccccccccccccccccccccccccccccce', 'DEPLOYED', 'USDC', 6, 'b1WHEEL', 18);

INSERT INTO v2_fund_state (chain_id, fund_address, last_event_block)
SELECT chain_id, fund_address, 1 FROM v2_fund_registry WHERE chain_id = 84532;
