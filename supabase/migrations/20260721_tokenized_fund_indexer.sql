CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS v2_fund_registry (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL CHECK (fund_address = lower(fund_address)),
    fund_key TEXT NOT NULL,
    start_block BIGINT NOT NULL CHECK (start_block > 0),
    accounting_asset TEXT NOT NULL CHECK (accounting_asset = lower(accounting_asset)),
    share_token TEXT NOT NULL CHECK (share_token = lower(share_token)),
    weth TEXT NOT NULL CHECK (weth = lower(weth)),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, fund_address),
    UNIQUE (fund_key)
);

CREATE TABLE IF NOT EXISTS v2_fund_contracts (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL CHECK (fund_address = lower(fund_address)),
    contract_address TEXT NOT NULL CHECK (contract_address = lower(contract_address)),
    contract_role TEXT NOT NULL CHECK (contract_role IN (
        'fund_vault', 'fund_share', 'fund_accounting', 'fund_flow_manager',
        'strategy_manager', 'csp_adapter', 'controller', 'batch_settler',
        'claim_escrow', 'access_manager', 'address_book', 'csp_valuator',
        'margin_pool', 'nav_verifier', 'oracle', 'otoken_factory',
        'swap_router', 'whitelist'
    )),
    interface_version BIGINT NOT NULL CHECK (interface_version > 0),
    implementation_address TEXT CHECK (
        implementation_address IS NULL
        OR implementation_address = lower(implementation_address)
    ),
    CHECK (
        contract_role NOT IN (
            'fund_vault', 'fund_share', 'fund_accounting',
            'fund_flow_manager', 'strategy_manager', 'csp_adapter',
            'controller', 'batch_settler'
        ) OR implementation_address IS NOT NULL
    ),
    valid_from_block BIGINT NOT NULL CHECK (valid_from_block > 0),
    valid_to_block BIGINT CHECK (valid_to_block >= valid_from_block),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, fund_address, contract_address, valid_from_block),
    EXCLUDE USING gist (
        chain_id WITH =,
        fund_address WITH =,
        contract_address WITH =,
        int8range(valid_from_block, valid_to_block, '[]') WITH &&
    ),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS v2_fund_contracts_fund_idx
    ON v2_fund_contracts (chain_id, fund_address);
CREATE UNIQUE INDEX IF NOT EXISTS v2_fund_contracts_active_role_idx
    ON v2_fund_contracts (chain_id, fund_address, contract_role)
    WHERE valid_to_block IS NULL;

CREATE TABLE IF NOT EXISTS v2_chain_events (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    contract_address TEXT NOT NULL,
    contract_role TEXT NOT NULL,
    interface_version BIGINT NOT NULL,
    block_number BIGINT NOT NULL,
    block_hash TEXT NOT NULL,
    transaction_hash TEXT NOT NULL,
    transaction_index INTEGER NOT NULL,
    log_index INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, fund_address, transaction_hash, log_index),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS v2_chain_events_activity_idx
    ON v2_chain_events (
        chain_id, fund_address, block_number DESC, log_index DESC
    );
CREATE INDEX IF NOT EXISTS v2_chain_events_contract_idx
    ON v2_chain_events (chain_id, contract_address, block_number);

CREATE TABLE IF NOT EXISTS v2_indexer_checkpoints (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    indexer_name TEXT NOT NULL,
    next_block BIGINT NOT NULL,
    last_block_hash TEXT,
    last_window_from_block BIGINT,
    last_window_to_block BIGINT,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, fund_address, indexer_name),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v2_fund_state (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    accounting_asset TEXT,
    weth TEXT,
    net_assets NUMERIC(78, 0) NOT NULL DEFAULT 0,
    share_supply NUMERIC(78, 0) NOT NULL DEFAULT 0,
    reserved_claim_assets NUMERIC(78, 0) NOT NULL DEFAULT 0,
    nav_stale BOOLEAN NOT NULL DEFAULT TRUE,
    positions_hash TEXT,
    reporter_set_version BIGINT NOT NULL DEFAULT 0,
    reporter_threshold INTEGER NOT NULL DEFAULT 0,
    active_reporter_count INTEGER NOT NULL DEFAULT 0,
    active_reporters JSONB NOT NULL DEFAULT '[]'::jsonb,
    fee_recipient TEXT,
    management_fee_wad NUMERIC(78, 0) NOT NULL DEFAULT 0,
    performance_fee_bps INTEGER NOT NULL DEFAULT 0,
    high_water_mark NUMERIC(78, 0) NOT NULL DEFAULT 0,
    last_report_nonce BIGINT NOT NULL DEFAULT 0,
    nav_valid_after_block BIGINT,
    nav_valid_until_block BIGINT,
    last_event_block BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, fund_address),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v2_share_balances (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    shares NUMERIC(78, 0) NOT NULL CHECK (shares >= 0),
    PRIMARY KEY (chain_id, fund_address, wallet_address),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS v2_share_balances_wallet_idx
    ON v2_share_balances (chain_id, wallet_address);

CREATE TABLE IF NOT EXISTS v2_redemptions (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    controller_address TEXT NOT NULL,
    pending_shares NUMERIC(78, 0) NOT NULL CHECK (pending_shares >= 0),
    claimable_shares NUMERIC(78, 0) NOT NULL CHECK (claimable_shares >= 0),
    claimable_assets NUMERIC(78, 0) NOT NULL CHECK (claimable_assets >= 0),
    status TEXT NOT NULL CHECK (
        status IN ('none', 'pending', 'cancelled', 'claimable', 'claimed')
    ),
    last_event_block BIGINT NOT NULL,
    PRIMARY KEY (chain_id, fund_address, controller_address),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS v2_redemptions_controller_idx
    ON v2_redemptions (chain_id, controller_address);
CREATE INDEX IF NOT EXISTS v2_redemptions_pending_idx
    ON v2_redemptions (chain_id, fund_address, status)
    WHERE status IN ('pending', 'claimable');

CREATE TABLE IF NOT EXISTS v2_csp_positions (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    adapter_address TEXT NOT NULL,
    position_id NUMERIC(78, 0) NOT NULL,
    protocol_vault_id NUMERIC(78, 0) NOT NULL,
    otoken_address TEXT NOT NULL,
    market_maker_address TEXT NOT NULL,
    option_amount NUMERIC(78, 0) NOT NULL,
    collateral NUMERIC(78, 0) NOT NULL,
    premium_earned NUMERIC(78, 0) NOT NULL,
    collateral_returned NUMERIC(78, 0) NOT NULL,
    settlement_payout NUMERIC(78, 0) NOT NULL,
    payment NUMERIC(78, 0) NOT NULL,
    assigned_weth NUMERIC(78, 0) NOT NULL,
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN (
            'open', 'awaiting_physical_delivery', 'settled_otm',
            'assigned', 'cash_fallback'
        )
    ),
    lifecycle_hash TEXT NOT NULL,
    opened_block BIGINT NOT NULL,
    settled_block BIGINT,
    PRIMARY KEY (chain_id, fund_address, adapter_address, position_id),
    UNIQUE (chain_id, fund_address, adapter_address, protocol_vault_id),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS v2_csp_positions_active_idx
    ON v2_csp_positions (chain_id, fund_address, lifecycle)
    WHERE lifecycle IN ('open', 'awaiting_physical_delivery');

CREATE TABLE IF NOT EXISTS v2_fund_inventory (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    asset_address TEXT NOT NULL,
    bucket TEXT NOT NULL CHECK (
        bucket IN ('strategy_accounted', 'assigned', 'claim_reserved')
    ),
    amount NUMERIC(78, 0) NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (chain_id, fund_address, asset_address, bucket),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v2_fund_components (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    component_id TEXT NOT NULL,
    valuator_address TEXT NOT NULL,
    interface_version BIGINT NOT NULL,
    active BOOLEAN NOT NULL,
    nonce BIGINT NOT NULL DEFAULT 0,
    position_state_hash TEXT,
    last_event_block BIGINT NOT NULL,
    PRIMARY KEY (chain_id, fund_address, component_id),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v2_nav_reports (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    report_nonce BIGINT NOT NULL,
    report_hash TEXT,
    net_assets NUMERIC(78, 0) NOT NULL,
    fee_shares NUMERIC(78, 0) NOT NULL,
    valid_after_block BIGINT,
    valid_until_block BIGINT,
    block_number BIGINT NOT NULL,
    PRIMARY KEY (chain_id, fund_address, report_nonce),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v2_fund_activity (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    transaction_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number BIGINT NOT NULL,
    activity_type TEXT NOT NULL,
    wallet_address TEXT,
    payload JSONB NOT NULL,
    PRIMARY KEY (chain_id, fund_address, transaction_hash, log_index),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS v2_fund_activity_cursor_idx
    ON v2_fund_activity (
        chain_id, fund_address, block_number DESC, log_index DESC
    );
CREATE INDEX IF NOT EXISTS v2_fund_activity_wallet_idx
    ON v2_fund_activity (
        chain_id, fund_address, wallet_address, block_number DESC, log_index DESC
    ) WHERE wallet_address IS NOT NULL;

CREATE TABLE IF NOT EXISTS v2_fund_reconciliations (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    block_number BIGINT NOT NULL,
    block_hash TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    checks JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, fund_address, block_number),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

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
        DELETE FROM v2_nav_reports
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
        DELETE FROM v2_fund_activity
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
        SELECT * FROM jsonb_to_recordset(p_projection->'share_balances') AS x(
            chain_id BIGINT, fund_address TEXT, wallet_address TEXT,
            shares NUMERIC
        );
        INSERT INTO v2_redemptions
        SELECT * FROM jsonb_to_recordset(p_projection->'redemptions') AS x(
            chain_id BIGINT, fund_address TEXT, controller_address TEXT,
            pending_shares NUMERIC, claimable_shares NUMERIC,
            claimable_assets NUMERIC, status TEXT, last_event_block BIGINT
        );
        INSERT INTO v2_csp_positions
        SELECT * FROM jsonb_to_recordset(p_projection->'positions') AS x(
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
        SELECT * FROM jsonb_to_recordset(p_projection->'inventory') AS x(
            chain_id BIGINT, fund_address TEXT, asset_address TEXT,
            bucket TEXT, amount NUMERIC
        );
        INSERT INTO v2_fund_components
        SELECT * FROM jsonb_to_recordset(p_projection->'components') AS x(
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
        FROM jsonb_to_recordset(p_projection->'nav_reports') AS x(
            chain_id BIGINT, fund_address TEXT, report_nonce BIGINT,
            net_assets NUMERIC, valid_after_block BIGINT,
            valid_until_block BIGINT, report_hash TEXT, fee_shares NUMERIC,
            block_number BIGINT
        );
        INSERT INTO v2_fund_activity
        SELECT * FROM jsonb_to_recordset(p_projection->'activities') AS x(
            chain_id BIGINT, fund_address TEXT, transaction_hash TEXT,
            log_index INTEGER, block_number BIGINT, activity_type TEXT,
            wallet_address TEXT, payload JSONB
        );

        INSERT INTO v2_fund_reconciliations (
            chain_id, fund_address, block_number, block_hash, passed, checks
        )
        SELECT
            x.chain_id, x.fund_address, x.block_number, x.block_hash,
            x.passed, x.checks
        FROM jsonb_to_recordset(
            coalesce(p_projection->'reconciliations', '[]'::jsonb)
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
    PERFORM 1 FROM v2_indexer_checkpoints
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND indexer_name = p_indexer_name
    FOR UPDATE;

    DELETE FROM v2_chain_events
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND block_number >= p_rewind_block;

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
    DELETE FROM v2_nav_reports
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_fund_activity
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_fund_state
    WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    DELETE FROM v2_fund_reconciliations
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND block_number >= p_rewind_block;

    UPDATE v2_indexer_checkpoints
    SET next_block = p_rewind_block,
        last_block_hash = NULL,
        last_window_from_block = NULL,
        last_window_to_block = NULL,
        last_error = 'reorg rewind',
        updated_at = now()
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND indexer_name = p_indexer_name;
END;
$$;
