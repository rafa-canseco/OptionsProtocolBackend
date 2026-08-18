-- B1N-388: keep the fund indexer bounded as NAV history grows.

CREATE INDEX IF NOT EXISTS v2_chain_events_state_replay_idx
    ON v2_chain_events (
        chain_id, fund_address, block_number, transaction_index, log_index
    )
    WHERE event_name NOT IN ('NavCommitted', 'NavSubmitted');

CREATE INDEX IF NOT EXISTS v2_chain_events_latest_nav_idx
    ON v2_chain_events (
        chain_id, fund_address, event_name, block_number DESC,
        transaction_index DESC, log_index DESC
    )
    WHERE event_name IN ('NavCommitted', 'NavSubmitted');

CREATE OR REPLACE FUNCTION v2_ingest_fund_window_b1n340(
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
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    expected_block BIGINT;
    committed_from_block BIGINT;
    committed_to_block BIGINT;
    committed_block_hash TEXT;
BEGIN
    INSERT INTO v2_indexer_checkpoints (
        chain_id, fund_address, indexer_name, next_block
    )
    SELECT p_chain_id, p_fund_address, p_indexer_name, start_block
    FROM v2_fund_registry
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address
    ON CONFLICT (chain_id, fund_address, indexer_name) DO NOTHING;

    SELECT
        next_block, last_window_from_block, last_window_to_block, last_block_hash
    INTO
        expected_block, committed_from_block, committed_to_block,
        committed_block_hash
    FROM v2_indexer_checkpoints
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND indexer_name = p_indexer_name
    FOR UPDATE;

    IF expected_block IS NULL THEN
        RAISE EXCEPTION 'Fund registry does not exist: %:%',
            p_chain_id, p_fund_address;
    END IF;
    IF expected_block = p_to_block + 1
       AND committed_from_block = p_from_block
       AND committed_to_block = p_to_block THEN
        IF committed_block_hash IS DISTINCT FROM p_last_block_hash THEN
            RAISE EXCEPTION 'Replayed fund window hash mismatch: expected %, received %',
                committed_block_hash, p_last_block_hash;
        END IF;
        RETURN;
    END IF;
    IF expected_block <> p_from_block OR p_to_block < p_from_block THEN
        RAISE EXCEPTION 'Non-contiguous fund window: expected %, received %-%',
            expected_block, p_from_block, p_to_block;
    END IF;

    INSERT INTO v2_chain_events (
        chain_id, fund_address, contract_address, contract_role,
        interface_version, block_number, block_hash, transaction_hash,
        transaction_index, log_index, event_name, payload
    )
    SELECT
        x.chain_id, x.fund_address, x.contract_address, x.contract_role,
        x.interface_version, x.block_number, x.block_hash,
        x.transaction_hash, x.transaction_index, x.log_index,
        x.event_name, x.payload
    FROM jsonb_to_recordset(p_events) AS x(
        chain_id BIGINT, fund_address TEXT, contract_address TEXT,
        contract_role TEXT, interface_version BIGINT, block_number BIGINT,
        block_hash TEXT, transaction_hash TEXT, transaction_index INTEGER,
        log_index INTEGER, event_name TEXT, payload JSONB
    )
    ON CONFLICT (
        chain_id, fund_address, transaction_hash, log_index
    ) DO NOTHING;

    IF p_projection ? 'fund_state' THEN
        DELETE FROM v2_share_balances
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
        DELETE FROM v2_redemptions
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
        DELETE FROM v2_csp_positions
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
        DELETE FROM v2_fund_inventory
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
        DELETE FROM v2_fund_components
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;

        INSERT INTO v2_fund_state (
            chain_id, fund_address, accounting_asset, weth, net_assets,
            share_supply, reserved_claim_assets, nav_stale, positions_hash,
            reporter_set_version, reporter_threshold, active_reporter_count,
            active_reporters,
            fee_recipient, management_fee_wad, performance_fee_bps,
            high_water_mark,
            last_report_nonce, nav_valid_after_block, nav_valid_until_block,
            last_event_block
        )
        SELECT * FROM jsonb_to_recordset(p_projection->'fund_state') AS x(
            chain_id BIGINT, fund_address TEXT, accounting_asset TEXT,
            weth TEXT, net_assets NUMERIC, share_supply NUMERIC,
            reserved_claim_assets NUMERIC, nav_stale BOOLEAN,
            positions_hash TEXT, reporter_set_version BIGINT,
            reporter_threshold INTEGER, active_reporter_count INTEGER,
            active_reporters JSONB,
            fee_recipient TEXT, management_fee_wad NUMERIC,
            performance_fee_bps INTEGER, high_water_mark NUMERIC,
            last_report_nonce BIGINT,
            nav_valid_after_block BIGINT, nav_valid_until_block BIGINT,
            last_event_block BIGINT
        )
        ON CONFLICT (chain_id, fund_address) DO UPDATE SET
            accounting_asset = EXCLUDED.accounting_asset,
            weth = EXCLUDED.weth,
            net_assets = EXCLUDED.net_assets,
            share_supply = EXCLUDED.share_supply,
            reserved_claim_assets = EXCLUDED.reserved_claim_assets,
            nav_stale = EXCLUDED.nav_stale,
            positions_hash = EXCLUDED.positions_hash,
            reporter_set_version = EXCLUDED.reporter_set_version,
            reporter_threshold = EXCLUDED.reporter_threshold,
            active_reporter_count = EXCLUDED.active_reporter_count,
            active_reporters = EXCLUDED.active_reporters,
            fee_recipient = EXCLUDED.fee_recipient,
            management_fee_wad = EXCLUDED.management_fee_wad,
            performance_fee_bps = EXCLUDED.performance_fee_bps,
            high_water_mark = EXCLUDED.high_water_mark,
            last_report_nonce = EXCLUDED.last_report_nonce,
            nav_valid_after_block = EXCLUDED.nav_valid_after_block,
            nav_valid_until_block = EXCLUDED.nav_valid_until_block,
            last_event_block = EXCLUDED.last_event_block,
            updated_at = now();

        INSERT INTO v2_share_balances
        SELECT * FROM jsonb_to_recordset(
            COALESCE(p_projection->'share_balances', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, wallet_address TEXT,
            shares NUMERIC
        );
        INSERT INTO v2_redemptions
        SELECT * FROM jsonb_to_recordset(
            COALESCE(p_projection->'redemptions', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, controller_address TEXT,
            pending_shares NUMERIC, claimable_shares NUMERIC,
            claimable_assets NUMERIC, status TEXT, last_event_block BIGINT
        );
        INSERT INTO v2_csp_positions
        SELECT * FROM jsonb_to_recordset(
            COALESCE(p_projection->'positions', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, adapter_address TEXT,
            position_id NUMERIC,
            protocol_vault_id NUMERIC, otoken_address TEXT,
            market_maker_address TEXT, option_amount NUMERIC,
            collateral NUMERIC, premium_earned NUMERIC,
            collateral_returned NUMERIC, settlement_payout NUMERIC,
            payment NUMERIC,
            assigned_weth NUMERIC, lifecycle TEXT, lifecycle_hash TEXT,
            opened_block BIGINT, settled_block BIGINT
        );
        INSERT INTO v2_fund_inventory
        SELECT * FROM jsonb_to_recordset(
            COALESCE(p_projection->'inventory', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, asset_address TEXT,
            bucket TEXT, amount NUMERIC
        );
        INSERT INTO v2_fund_components
        SELECT * FROM jsonb_to_recordset(
            COALESCE(p_projection->'components', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, component_id TEXT,
            valuator_address TEXT, interface_version BIGINT, active BOOLEAN,
            nonce BIGINT, position_state_hash TEXT, last_event_block BIGINT
        );

        INSERT INTO v2_nav_reports (
            chain_id, fund_address, report_nonce, report_hash, net_assets,
            fee_shares, valid_after_block, valid_until_block, block_number
        )
        SELECT
            x.chain_id, x.fund_address, x.report_nonce, x.report_hash,
            x.net_assets, x.fee_shares, x.valid_after_block,
            x.valid_until_block, x.block_number
        FROM jsonb_to_recordset(
            COALESCE(p_projection->'nav_reports', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, report_nonce BIGINT,
            net_assets NUMERIC, valid_after_block BIGINT,
            valid_until_block BIGINT, report_hash TEXT, fee_shares NUMERIC,
            block_number BIGINT
        )
        ON CONFLICT (chain_id, fund_address, report_nonce) DO UPDATE SET
            report_hash = EXCLUDED.report_hash,
            net_assets = EXCLUDED.net_assets,
            fee_shares = EXCLUDED.fee_shares,
            valid_after_block = EXCLUDED.valid_after_block,
            valid_until_block = EXCLUDED.valid_until_block,
            block_number = EXCLUDED.block_number;

        INSERT INTO v2_fund_activity (
            chain_id, fund_address, transaction_hash, log_index,
            block_number, activity_type, wallet_address, payload
        )
        SELECT
            x.chain_id, x.fund_address, x.transaction_hash, x.log_index,
            x.block_number, x.activity_type, x.wallet_address, x.payload
        FROM jsonb_to_recordset(
            COALESCE(p_projection->'activities', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, transaction_hash TEXT,
            log_index INTEGER, block_number BIGINT, activity_type TEXT,
            wallet_address TEXT, payload JSONB
        )
        ON CONFLICT (
            chain_id, fund_address, transaction_hash, log_index
        ) DO UPDATE SET
            block_number = EXCLUDED.block_number,
            activity_type = EXCLUDED.activity_type,
            wallet_address = EXCLUDED.wallet_address,
            payload = EXCLUDED.payload;

        INSERT INTO v2_fund_reconciliations (
            chain_id, fund_address, block_number, block_hash, passed, checks
        )
        SELECT
            x.chain_id, x.fund_address, x.block_number, x.block_hash,
            x.passed, x.checks
        FROM jsonb_to_recordset(
            COALESCE(p_projection->'reconciliations', '[]'::jsonb)
        ) AS x(
            chain_id BIGINT, fund_address TEXT, block_number BIGINT,
            block_hash TEXT, passed BOOLEAN, checks JSONB
        )
        ON CONFLICT (chain_id, fund_address, block_number) DO UPDATE SET
            block_hash = EXCLUDED.block_hash,
            passed = EXCLUDED.passed,
            checks = EXCLUDED.checks,
            created_at = now();
    END IF;

    UPDATE v2_indexer_checkpoints
    SET next_block = p_to_block + 1,
        last_block_hash = p_last_block_hash,
        last_window_from_block = p_from_block,
        last_window_to_block = p_to_block,
        last_error = NULL,
        updated_at = now()
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND indexer_name = p_indexer_name;
END;
$$;

REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n340(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) FROM PUBLIC;
