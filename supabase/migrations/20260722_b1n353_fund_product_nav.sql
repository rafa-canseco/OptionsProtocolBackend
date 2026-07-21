ALTER TABLE v2_fund_registry
    ADD COLUMN deployment_status TEXT NOT NULL DEFAULT 'NOT_DEPLOYED'
        CHECK (deployment_status IN ('NOT_DEPLOYED', 'DEPLOYED', 'RETIRED')),
    ADD COLUMN accounting_asset_symbol TEXT,
    ADD COLUMN accounting_asset_decimals INTEGER
        CHECK (accounting_asset_decimals BETWEEN 0 AND 255),
    ADD COLUMN share_symbol TEXT,
    ADD COLUMN share_decimals INTEGER CHECK (share_decimals BETWEEN 0 AND 255);

UPDATE v2_fund_registry SET enabled = FALSE WHERE enabled;

ALTER TABLE v2_fund_registry
    ADD CONSTRAINT v2_fund_registry_enabled_metadata CHECK (
        NOT enabled OR (
            deployment_status = 'DEPLOYED'
            AND accounting_asset_symbol IS NOT NULL
            AND accounting_asset_decimals IS NOT NULL
            AND share_symbol IS NOT NULL
            AND share_decimals IS NOT NULL
        )
    );

ALTER TABLE v2_fund_state
    ADD COLUMN accounted_idle_assets NUMERIC(78, 0) NOT NULL DEFAULT 0,
    ADD COLUMN virtual_shares NUMERIC(78, 0) NOT NULL DEFAULT 0,
    ADD COLUMN deposits_paused BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN redemptions_paused BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN execution_lock_owner TEXT,
    ADD COLUMN has_active_processing BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN fund_flow_nonce BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN idle_state_hash TEXT,
    ADD COLUMN as_of_block BIGINT,
    ADD COLUMN as_of_block_hash TEXT,
    ADD COLUMN reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN indexed_at TIMESTAMPTZ;

CREATE TABLE v2_redemption_batch_states (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL,
    controller_address TEXT NOT NULL,
    latest_batch_id BIGINT NOT NULL CHECK (latest_batch_id >= 0),
    processing BOOLEAN NOT NULL,
    unwind_committed BOOLEAN NOT NULL,
    PRIMARY KEY (chain_id, fund_address, controller_address),
    FOREIGN KEY (chain_id, fund_address, controller_address)
        REFERENCES v2_redemptions (
            chain_id, fund_address, controller_address
        ) ON DELETE CASCADE
);

CREATE TABLE v2_confirmed_chain_heads (
    chain_id BIGINT PRIMARY KEY,
    block_number BIGINT NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE v2_csp_option_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL CHECK (fund_address = lower(fund_address)),
    valuator_address TEXT NOT NULL CHECK (valuator_address = lower(valuator_address)),
    adapter_address TEXT NOT NULL CHECK (adapter_address = lower(adapter_address)),
    position_id NUMERIC(78, 0) NOT NULL,
    snapshot_block BIGINT NOT NULL,
    snapshot_block_hash TEXT NOT NULL,
    valid_until_block BIGINT NOT NULL,
    liability NUMERIC(78, 0) NOT NULL CHECK (liability >= 0),
    base_exit_cost NUMERIC(78, 0) NOT NULL CHECK (base_exit_cost >= 0),
    observation_nonce NUMERIC(78, 0) NOT NULL,
    digest TEXT NOT NULL,
    observer_address TEXT NOT NULL CHECK (observer_address = lower(observer_address)),
    market_maker_address TEXT NOT NULL CHECK (
        market_maker_address = lower(market_maker_address)
    ),
    signature TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE,
    UNIQUE (
        chain_id, valuator_address, adapter_address, position_id,
        snapshot_block, observer_address
    ),
    UNIQUE (chain_id, valuator_address, digest)
);

CREATE INDEX v2_csp_option_observations_snapshot_idx
    ON v2_csp_option_observations (
        chain_id, fund_address, adapter_address, snapshot_block, position_id
    );

CREATE TABLE v2_nav_report_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chain_id BIGINT,
    fund_address TEXT,
    snapshot_block BIGINT,
    snapshot_block_hash TEXT,
    report_nonce BIGINT,
    status TEXT NOT NULL CHECK (
        status IN (
            'building', 'blocked', 'simulated', 'reconciling',
            'submitted', 'confirmed', 'failed'
        )
    ),
    reason_code TEXT,
    reports JSONB NOT NULL DEFAULT '[]'::jsonb,
    reporters JSONB NOT NULL DEFAULT '[]'::jsonb,
    signatures JSONB NOT NULL DEFAULT '[]'::jsonb,
    transaction_hash TEXT,
    signed_transaction TEXT,
    ownership_token UUID,
    lease_expires_at TIMESTAMPTZ,
    error TEXT,
    failed_transaction_hash TEXT,
    failed_transaction_error TEXT,
    failed_transaction_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE,
    CHECK ((transaction_hash IS NULL) = (signed_transaction IS NULL))
);

ALTER TABLE v2_nav_report_runs ENABLE ROW LEVEL SECURITY;

CREATE UNIQUE INDEX v2_nav_report_runs_attempt_idx
    ON v2_nav_report_runs (chain_id, fund_address, report_nonce);

CREATE FUNCTION v2_claim_nav_report_run(
    p_chain_id BIGINT,
    p_fund_address TEXT,
    p_snapshot_block BIGINT,
    p_snapshot_block_hash TEXT,
    p_report_nonce BIGINT,
    p_ownership_token UUID,
    p_lease_seconds INTEGER
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    claimed BOOLEAN := FALSE;
    run v2_nav_report_runs%ROWTYPE;
BEGIN
    IF p_ownership_token IS NULL
       OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 15 AND 900 THEN
        RAISE EXCEPTION 'Invalid NAV report-run claim lease';
    END IF;

    INSERT INTO v2_nav_report_runs (
        chain_id, fund_address, snapshot_block, snapshot_block_hash,
        report_nonce, status, ownership_token, lease_expires_at
    ) VALUES (
        p_chain_id, p_fund_address, p_snapshot_block, p_snapshot_block_hash,
        p_report_nonce, 'building', p_ownership_token,
        now() + make_interval(secs => p_lease_seconds)
    )
    ON CONFLICT (chain_id, fund_address, report_nonce) DO NOTHING;

    SELECT * INTO run
    FROM v2_nav_report_runs
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND report_nonce = p_report_nonce
    FOR UPDATE;

    IF run.ownership_token = p_ownership_token THEN
        claimed := TRUE;
    ELSIF run.transaction_hash IS NULL
          AND run.signed_transaction IS NULL
          AND (
              run.status IN ('blocked', 'failed')
              OR (
                  run.status IN ('building', 'simulated')
                  AND coalesce(run.lease_expires_at, '-infinity') <= now()
              )
          ) THEN
        UPDATE v2_nav_report_runs SET
            snapshot_block = p_snapshot_block,
            snapshot_block_hash = p_snapshot_block_hash,
            status = 'building',
            reason_code = NULL,
            reports = '[]'::jsonb,
            reporters = '[]'::jsonb,
            signatures = '[]'::jsonb,
            error = NULL,
            ownership_token = p_ownership_token,
            lease_expires_at = now() + make_interval(secs => p_lease_seconds),
            updated_at = now()
        WHERE id = run.id;
        claimed := TRUE;
    END IF;

    SELECT * INTO run FROM v2_nav_report_runs WHERE id = run.id;
    RETURN jsonb_build_object(
        'owned', claimed, 'run', to_jsonb(run) - 'ownership_token'
    );
END;
$$;

CREATE FUNCTION v2_update_nav_report_run(
    p_run_id UUID,
    p_ownership_token UUID,
    p_lease_seconds INTEGER,
    p_run JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 15 AND 900 THEN
        RAISE EXCEPTION 'Invalid NAV report-run update lease';
    END IF;

    UPDATE v2_nav_report_runs SET
        status = p_run->>'status',
        reason_code = p_run->>'reason_code',
        transaction_hash = coalesce(
            transaction_hash, p_run->>'transaction_hash'
        ),
        signed_transaction = coalesce(
            signed_transaction, p_run->>'signed_transaction'
        ),
        reports = coalesce(p_run->'reports', '[]'::jsonb),
        reporters = coalesce(p_run->'reporters', '[]'::jsonb),
        signatures = coalesce(p_run->'signatures', '[]'::jsonb),
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        updated_at = now()
    WHERE id = p_run_id
      AND ownership_token = p_ownership_token
      AND (
          lease_expires_at > now()
          OR (transaction_hash IS NOT NULL AND signed_transaction IS NOT NULL)
      )
      AND (
          transaction_hash IS NULL
          OR p_run->>'transaction_hash' IS NULL
          OR transaction_hash = p_run->>'transaction_hash'
      )
      AND (
          signed_transaction IS NULL
          OR p_run->>'signed_transaction' IS NULL
          OR signed_transaction = p_run->>'signed_transaction'
      );
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE FUNCTION v2_record_reverted_nav_report_run(
    p_chain_id BIGINT,
    p_fund_address TEXT,
    p_report_nonce BIGINT,
    p_run_id UUID,
    p_transaction_hash TEXT,
    p_error TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE v2_nav_report_runs SET
        status = 'failed',
        reason_code = 'TRANSACTION_REVERTED',
        failed_transaction_hash = transaction_hash,
        failed_transaction_error = coalesce(
            p_error, 'CONFIRMED_RECEIPT_STATUS_0'
        ),
        failed_transaction_at = now(),
        transaction_hash = NULL,
        signed_transaction = NULL,
        ownership_token = NULL,
        lease_expires_at = NULL,
        updated_at = now()
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND report_nonce = p_report_nonce
      AND id = p_run_id
      AND status IN ('reconciling', 'submitted')
      AND transaction_hash = p_transaction_hash
      AND signed_transaction IS NOT NULL;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

REVOKE EXECUTE ON FUNCTION v2_claim_nav_report_run(
    BIGINT, TEXT, BIGINT, TEXT, BIGINT, UUID, INTEGER
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION v2_update_nav_report_run(
    UUID, UUID, INTEGER, JSONB
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION v2_record_reverted_nav_report_run(
    BIGINT, TEXT, BIGINT, UUID, TEXT, TEXT
) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT ALL ON v2_nav_report_runs TO service_role;
        GRANT EXECUTE ON FUNCTION v2_claim_nav_report_run(
            BIGINT, TEXT, BIGINT, TEXT, BIGINT, UUID, INTEGER
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v2_update_nav_report_run(
            UUID, UUID, INTEGER, JSONB
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v2_record_reverted_nav_report_run(
            BIGINT, TEXT, BIGINT, UUID, TEXT, TEXT
        ) TO service_role;
    END IF;
END;
$access$;

ALTER FUNCTION v2_ingest_fund_window(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) RENAME TO v2_ingest_fund_window_b1n340;

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
    state JSONB;
BEGIN
    PERFORM v2_ingest_fund_window_b1n340(
        p_chain_id, p_fund_address, p_indexer_name, p_from_block,
        p_to_block, p_last_block_hash, p_events, p_projection
    );

    state := p_projection->'fund_state'->0;
    IF state IS NOT NULL THEN
        UPDATE v2_fund_state SET
            accounted_idle_assets = (state->>'accounted_idle_assets')::NUMERIC,
            virtual_shares = (state->>'virtual_shares')::NUMERIC,
            deposits_paused = (state->>'deposits_paused')::BOOLEAN,
            redemptions_paused = (state->>'redemptions_paused')::BOOLEAN,
            execution_lock_owner = state->>'execution_lock_owner',
            has_active_processing = (state->>'has_active_processing')::BOOLEAN,
            fund_flow_nonce = (state->>'fund_flow_nonce')::BIGINT,
            idle_state_hash = state->>'idle_state_hash',
            as_of_block = (state->>'as_of_block')::BIGINT,
            as_of_block_hash = state->>'as_of_block_hash',
            reconciled = (state->>'reconciled')::BOOLEAN,
            indexed_at = (state->>'indexed_at')::TIMESTAMPTZ
        WHERE chain_id = p_chain_id AND fund_address = p_fund_address;
    END IF;

    INSERT INTO v2_redemption_batch_states (
        chain_id, fund_address, controller_address, latest_batch_id,
        processing, unwind_committed
    )
    SELECT
        x.chain_id, x.fund_address, x.controller_address, x.latest_batch_id,
        x.processing, x.unwind_committed
    FROM jsonb_to_recordset(
        coalesce(p_projection->'redemption_batch_states', '[]'::jsonb)
    ) AS x(
        chain_id BIGINT, fund_address TEXT, controller_address TEXT,
        latest_batch_id BIGINT, processing BOOLEAN, unwind_committed BOOLEAN
    )
    ON CONFLICT (chain_id, fund_address, controller_address) DO UPDATE SET
        latest_batch_id = EXCLUDED.latest_batch_id,
        processing = EXCLUDED.processing,
        unwind_committed = EXCLUDED.unwind_committed;
END;
$$;
