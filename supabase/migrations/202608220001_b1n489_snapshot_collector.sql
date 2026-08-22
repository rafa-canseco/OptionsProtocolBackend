CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SEQUENCE IF NOT EXISTS v2_snapshot_generation_seq;

CREATE TABLE IF NOT EXISTS v2_snapshot_collector_control (
    environment TEXT NOT NULL,
    chain_id BIGINT NOT NULL,
    failure_streak INTEGER NOT NULL DEFAULT 0 CHECK (failure_streak >= 0),
    circuit_open_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (environment, chain_id)
);

CREATE TABLE IF NOT EXISTS v2_snapshot_windows (
    environment TEXT NOT NULL,
    chain_id BIGINT NOT NULL,
    window_id BIGINT NOT NULL,
    claim_token UUID NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'published', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    retry_call_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_call_count BETWEEN 0 AND 4),
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    publish_deadline TIMESTAMPTZ NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    failure_code TEXT,
    generation BIGINT,
    PRIMARY KEY (environment, chain_id, window_id),
    UNIQUE (claim_token),
    CHECK (publish_deadline = window_end),
    CHECK (window_start < window_end)
);

CREATE TABLE IF NOT EXISTS v2_snapshot_event_backfills (
    environment TEXT NOT NULL,
    chain_id BIGINT NOT NULL,
    from_block BIGINT NOT NULL,
    to_block BIGINT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (environment, chain_id, from_block, to_block),
    CHECK (from_block >= 0 AND to_block >= from_block)
);

CREATE TABLE IF NOT EXISTS v2_snapshot_envelopes (
    environment TEXT NOT NULL,
    chain_id BIGINT NOT NULL,
    window_id BIGINT NOT NULL,
    generation BIGINT NOT NULL DEFAULT nextval('v2_snapshot_generation_seq'),
    snapshot_block BIGINT NOT NULL CHECK (snapshot_block >= 0),
    snapshot_block_hash TEXT NOT NULL,
    snapshot_block_timestamp TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    reconciled BOOLEAN NOT NULL,
    common JSONB NOT NULL CHECK (jsonb_typeof(common) = 'object'),
    funds JSONB NOT NULL CHECK (jsonb_typeof(funds) = 'array'),
    PRIMARY KEY (environment, chain_id, generation),
    UNIQUE (environment, chain_id, window_id),
    FOREIGN KEY (environment, chain_id, window_id)
        REFERENCES v2_snapshot_windows (environment, chain_id, window_id)
);

CREATE TABLE IF NOT EXISTS v2_snapshot_current (
    environment TEXT NOT NULL,
    chain_id BIGINT NOT NULL,
    window_id BIGINT NOT NULL,
    generation BIGINT NOT NULL,
    snapshot_block BIGINT NOT NULL,
    PRIMARY KEY (environment, chain_id),
    FOREIGN KEY (environment, chain_id, generation)
        REFERENCES v2_snapshot_envelopes (environment, chain_id, generation)
);

ALTER TABLE v2_fund_state
    ADD COLUMN IF NOT EXISTS snapshot_generation BIGINT,
    ADD COLUMN IF NOT EXISTS snapshot_published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS snapshot_state JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION v2_claim_snapshot_window(
    p_environment TEXT,
    p_chain_id BIGINT
) RETURNS TABLE (
    claim_token UUID,
    window_id BIGINT,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    publish_deadline TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    db_now TIMESTAMPTZ := clock_timestamp();
    candidate_window BIGINT := floor(extract(epoch FROM db_now) / 30)::BIGINT;
    bucket_start TIMESTAMPTZ := to_timestamp(candidate_window * 30);
    bucket_end TIMESTAMPTZ := to_timestamp((candidate_window + 1) * 30);
    control_row v2_snapshot_collector_control%ROWTYPE;
BEGIN
    IF p_environment IS NULL OR btrim(p_environment) = '' OR p_chain_id <= 0 THEN
        RAISE EXCEPTION 'environment and positive chain_id are required';
    END IF;

    INSERT INTO v2_snapshot_collector_control (environment, chain_id)
    VALUES (p_environment, p_chain_id)
    ON CONFLICT DO NOTHING;

    SELECT * INTO control_row
    FROM v2_snapshot_collector_control c
    WHERE c.environment = p_environment AND c.chain_id = p_chain_id
    FOR UPDATE;

    IF control_row.circuit_open_until IS NOT NULL
       AND control_row.circuit_open_until > db_now THEN
        RETURN;
    END IF;
    IF extract(epoch FROM bucket_end - db_now) < 10 THEN
        RETURN;
    END IF;

    RETURN QUERY
    INSERT INTO v2_snapshot_windows AS w (
        environment, chain_id, window_id, claim_token, status,
        window_start, window_end, publish_deadline, claimed_at
    ) VALUES (
        p_environment, p_chain_id, candidate_window, gen_random_uuid(), 'claimed',
        bucket_start, bucket_end, bucket_end, db_now
    )
    ON CONFLICT ON CONSTRAINT v2_snapshot_windows_pkey DO NOTHING
    RETURNING w.claim_token, w.window_id, w.window_start, w.window_end,
              w.publish_deadline;
END;
$$;

CREATE OR REPLACE FUNCTION v2_reserve_snapshot_retry(
    p_environment TEXT,
    p_chain_id BIGINT,
    p_window_id BIGINT,
    p_claim_token UUID
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    reserved BOOLEAN;
BEGIN
    UPDATE v2_snapshot_windows w
    SET retry_call_count = retry_call_count + 1,
        attempt_count = attempt_count + 1
    WHERE w.environment = p_environment
      AND w.chain_id = p_chain_id
      AND w.window_id = p_window_id
      AND w.claim_token = p_claim_token
      AND w.status = 'claimed'
      AND clock_timestamp() < w.publish_deadline
      AND w.retry_call_count < 4
    RETURNING TRUE INTO reserved;
    RETURN coalesce(reserved, FALSE);
END;
$$;

CREATE OR REPLACE FUNCTION v2_fail_snapshot_window(
    p_environment TEXT,
    p_chain_id BIGINT,
    p_window_id BIGINT,
    p_claim_token UUID,
    p_failure_code TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    changed BOOLEAN;
    db_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    UPDATE v2_snapshot_windows w
    SET status = 'failed', failed_at = db_now,
        failure_code = left(coalesce(p_failure_code, 'snapshot_failed'), 80)
    WHERE w.environment = p_environment
      AND w.chain_id = p_chain_id
      AND w.window_id = p_window_id
      AND w.claim_token = p_claim_token
      AND w.status = 'claimed'
    RETURNING TRUE INTO changed;

    IF coalesce(changed, FALSE) THEN
        INSERT INTO v2_snapshot_collector_control AS c (
            environment, chain_id, failure_streak, circuit_open_until, updated_at
        ) VALUES (p_environment, p_chain_id, 1, NULL, db_now)
        ON CONFLICT (environment, chain_id) DO UPDATE SET
            failure_streak = c.failure_streak + 1,
            circuit_open_until = CASE
                WHEN c.failure_streak + 1 >= 3
                    THEN greatest(coalesce(c.circuit_open_until, db_now), db_now + interval '60 seconds')
                ELSE c.circuit_open_until
            END,
            updated_at = db_now;
    END IF;
    RETURN coalesce(changed, FALSE);
END;
$$;

CREATE OR REPLACE FUNCTION v2_claim_snapshot_event_backfill(
    p_environment TEXT,
    p_chain_id BIGINT
) RETURNS SETOF v2_snapshot_event_backfills
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    UPDATE v2_snapshot_event_backfills work
    SET status = 'running'
    WHERE (work.environment, work.chain_id, work.from_block, work.to_block) = (
        SELECT pending.environment, pending.chain_id,
               pending.from_block, pending.to_block
        FROM v2_snapshot_event_backfills pending
        WHERE pending.environment = p_environment
          AND pending.chain_id = p_chain_id
          AND pending.status IN ('pending', 'failed')
        ORDER BY pending.from_block
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING work.*;
END;
$$;

CREATE OR REPLACE FUNCTION v2_finish_snapshot_event_backfill(
    p_environment TEXT,
    p_chain_id BIGINT,
    p_from_block BIGINT,
    p_to_block BIGINT,
    p_succeeded BOOLEAN
) RETURNS BOOLEAN
LANGUAGE sql
SECURITY INVOKER
SET search_path = public
AS $$
    UPDATE v2_snapshot_event_backfills work
    SET status = CASE WHEN p_succeeded THEN 'done' ELSE 'failed' END
    WHERE work.environment = p_environment
      AND work.chain_id = p_chain_id
      AND work.from_block = p_from_block
      AND work.to_block = p_to_block
      AND work.status = 'running'
    RETURNING TRUE;
$$;

CREATE OR REPLACE FUNCTION v2_rewind_snapshot_event_backfill(
    p_chain_id BIGINT,
    p_fund_addresses JSONB,
    p_rewind_block BIGINT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    fund_address TEXT;
BEGIN
    IF jsonb_typeof(p_fund_addresses) IS DISTINCT FROM 'array'
       OR p_rewind_block <= 0 THEN
        RAISE EXCEPTION 'invalid backfill rewind request';
    END IF;
    FOR fund_address IN
        SELECT lower(value #>> '{}') FROM jsonb_array_elements(p_fund_addresses)
    LOOP
        PERFORM v2_rewind_fund_indexer(
            p_chain_id, fund_address, 'tokenized_csp_fund', p_rewind_block
        );
    END LOOP;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION v2_apply_snapshot_event_backfill_chunk(
    p_environment TEXT,
    p_chain_id BIGINT,
    p_from_block BIGINT,
    p_to_block BIGINT,
    p_ingestions JSONB,
    p_final_chunk BOOLEAN
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    ingestion JSONB;
BEGIN
    IF jsonb_typeof(p_ingestions) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'backfill ingestions must be an array';
    END IF;
    FOR ingestion IN SELECT value FROM jsonb_array_elements(p_ingestions)
    LOOP
        PERFORM v2_ingest_fund_window(
            (ingestion->>'chain_id')::BIGINT,
            lower(ingestion->>'fund_address'),
            ingestion->>'indexer_name',
            (ingestion->>'from_block')::BIGINT,
            (ingestion->>'to_block')::BIGINT,
            lower(ingestion->>'last_block_hash'),
            coalesce(ingestion->'events', '[]'::jsonb),
            ingestion->'projection'
        );
    END LOOP;
    IF p_final_chunk THEN
        UPDATE v2_snapshot_event_backfills work
        SET status = 'done'
        WHERE work.environment = p_environment
          AND work.chain_id = p_chain_id
          AND work.from_block = p_from_block
          AND work.to_block = p_to_block
          AND work.status = 'running';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'running backfill work was not found';
        END IF;
    END IF;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION v2_publish_snapshot(
    p_environment TEXT,
    p_chain_id BIGINT,
    p_window_id BIGINT,
    p_claim_token UUID,
    p_snapshot_block BIGINT,
    p_snapshot_block_hash TEXT,
    p_snapshot_block_timestamp TIMESTAMPTZ,
    p_common JSONB,
    p_funds JSONB,
    p_ingestions JSONB DEFAULT '[]'::jsonb
) RETURNS BIGINT
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    db_now TIMESTAMPTZ := clock_timestamp();
    current_row v2_snapshot_current%ROWTYPE;
    claim_deadline TIMESTAMPTZ;
    new_generation BIGINT;
    pointer_updated BOOLEAN;
    head_updated BOOLEAN;
    payload_fund_count INTEGER;
    distinct_payload_fund_count INTEGER;
    distinct_fund_type_count INTEGER;
    enabled_fund_count INTEGER;
    normalized_update_count INTEGER;
    fund JSONB;
    ingestion JSONB;
BEGIN
    IF p_snapshot_block < 0
       OR p_snapshot_block_hash IS NULL
       OR p_snapshot_block_timestamp IS NULL
       OR db_now - p_snapshot_block_timestamp > interval '45 seconds'
       OR jsonb_typeof(p_common) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_funds) IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_ingestions) IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_funds) < 1
       OR jsonb_array_length(p_funds) > 3 THEN
        RAISE EXCEPTION 'invalid snapshot envelope';
    END IF;
    SELECT count(*), count(DISTINCT lower(value->>'fund_address')),
           count(DISTINCT value->>'fund_type')
    INTO payload_fund_count, distinct_payload_fund_count, distinct_fund_type_count
    FROM jsonb_array_elements(p_funds);
    SELECT count(*) INTO enabled_fund_count
    FROM v2_fund_registry r
    WHERE r.chain_id = p_chain_id AND r.enabled
      AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(p_funds) f
          WHERE lower(f->>'fund_address') = r.fund_address
            AND f->>'fund_key' = r.fund_key
            AND f->>'fund_type' = r.strategy_kind
      );
    IF distinct_payload_fund_count <> payload_fund_count
       OR distinct_fund_type_count <> payload_fund_count
       OR enabled_fund_count <> payload_fund_count
       OR enabled_fund_count <> (
           SELECT count(*) FROM v2_fund_registry r
           WHERE r.chain_id = p_chain_id AND r.enabled
       ) THEN
        RAISE EXCEPTION 'snapshot funds do not exactly match enabled registry';
    END IF;
    IF p_common - ARRAY['market', 'quotes', 'market_maker'] <> '{}'::jsonb
       OR jsonb_typeof(p_common->'market') IS DISTINCT FROM 'object'
       OR NOT ((p_common->'market') ?& ARRAY[
           'asset', 'spot', 'iv', 'iv_source', 'observed_at',
           'protocol_fee_bps', 'available_otokens'
       ])
       OR (p_common->'market') - ARRAY[
           'asset', 'spot', 'iv', 'iv_source', 'observed_at',
           'protocol_fee_bps', 'available_otokens'
       ] <> '{}'::jsonb
       OR jsonb_typeof(p_common->'market'->'available_otokens') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_common->'quotes') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_common->'market_maker') IS DISTINCT FROM 'object'
       OR NOT ((p_common->'market_maker') ?& ARRAY[
           'mm_address', 'usdc_address', 'allowance_spender',
           'usdc_balance_raw', 'usdc_allowance_raw', 'maker_nonce'
       ])
       OR (p_common->'market_maker') - ARRAY[
           'mm_address', 'usdc_address', 'allowance_spender',
           'usdc_balance_raw', 'usdc_allowance_raw', 'maker_nonce'
       ] <> '{}'::jsonb
       OR p_common->'market_maker'->>'mm_address' !~ '^0x[0-9a-fA-F]{40}$'
       OR p_common->'market_maker'->>'usdc_address' !~ '^0x[0-9a-fA-F]{40}$'
       OR p_common->'market_maker'->>'allowance_spender' !~ '^0x[0-9a-fA-F]{40}$'
       OR EXISTS (
           SELECT 1 FROM jsonb_array_elements(p_common->'quotes') q
           WHERE NOT EXISTS (
               SELECT 1
               FROM jsonb_array_elements(p_common->'market'->'available_otokens') o
               WHERE lower(o->>'address') = lower(q->>'otoken_address')
           )
       ) THEN
        RAISE EXCEPTION 'invalid or incomplete shared snapshot state';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(
            p_common->'market'->'available_otokens'
        ) item
        WHERE NOT (item ?& ARRAY['address', 'strike_price', 'expiry', 'is_put'])
           OR item - ARRAY['address', 'strike_price', 'expiry', 'is_put'] <> '{}'::jsonb
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_common->'quotes') item
        WHERE NOT (item ?& ARRAY[
            'asset', 'chain', 'is_put', 'created_at', 'deadline', 'expiry',
            'strike_price', 'deployment_status', 'otoken_address', 'bid_price',
            'quote_id', 'max_amount', 'maker_nonce', 'signature'
        ])
           OR item - ARRAY[
               'asset', 'chain', 'is_put', 'created_at', 'deadline', 'expiry',
               'strike_price', 'deployment_status', 'otoken_address', 'bid_price',
               'quote_id', 'max_amount', 'maker_nonce', 'signature'
           ] <> '{}'::jsonb
    ) THEN
        RAISE EXCEPTION 'invalid common market or quote item';
    END IF;

    FOR fund IN SELECT value FROM jsonb_array_elements(p_funds)
    LOOP
        IF NOT (fund ?& ARRAY['fund_key', 'fund_type', 'fund_address', 'state'])
           OR fund - ARRAY['fund_key', 'fund_type', 'fund_address', 'state'] <> '{}'::jsonb
           OR jsonb_typeof(fund->'state') IS DISTINCT FROM 'object'
           OR jsonb_typeof(fund->'state'->'allocator') IS DISTINCT FROM 'object'
           OR (
               fund->>'fund_type' = 'csp'
               AND (
                   NOT ((fund->'state'->'allocator') ?& ARRAY[
                       'weth', 'nav', 'strategy_hash', 'strategy_config',
                       'adapter_config', 'adapter_state', 'valuation_policy',
                       'total_assets', 'idle_assets', 'allocated',
                       'minimum_idle_bps', 'processing', 'pending_shares',
                       'protocol_fee_bps', 'treasury', 'series', 'quote_states',
                       'position', 'position_expiry'
                   ])
                   OR (fund->'state'->'allocator') - ARRAY[
                       'weth', 'nav', 'strategy_hash', 'strategy_config',
                       'adapter_config', 'adapter_state', 'valuation_policy',
                       'total_assets', 'idle_assets', 'allocated',
                       'minimum_idle_bps', 'processing', 'pending_shares',
                       'protocol_fee_bps', 'treasury', 'series', 'quote_states',
                       'position', 'position_expiry'
                   ] <> '{}'::jsonb
               )
           )
           OR (
               fund->>'fund_type' = 'covered_call'
               AND (
                   NOT ((fund->'state'->'allocator') ?& ARRAY[
                       'nav', 'strategy_hash', 'strategy_config', 'adapter_config',
                       'adapter_state', 'positions', 'valuation_policy',
                       'valuation_observers', 'total_assets', 'idle_assets',
                       'allocated', 'minimum_idle_bps', 'processing',
                       'pending_shares', 'spot_price', 'protocol_fee_bps',
                       'series', 'position_expiries'
                   ])
                   OR (fund->'state'->'allocator') - ARRAY[
                       'nav', 'strategy_hash', 'strategy_config', 'adapter_config',
                       'adapter_state', 'positions', 'valuation_policy',
                       'valuation_observers', 'total_assets', 'idle_assets',
                       'allocated', 'minimum_idle_bps', 'processing',
                       'pending_shares', 'spot_price', 'protocol_fee_bps',
                       'series', 'position_expiries'
                   ] <> '{}'::jsonb
               )
           )
           OR (
               fund->>'fund_type' = 'meta_wheel'
               AND (
                   NOT ((fund->'state'->'allocator') ?& ARRAY['wheel_snapshot', 'wheel_quotes'])
                   OR (fund->'state'->'allocator') - ARRAY['wheel_snapshot', 'wheel_quotes'] <> '{}'::jsonb
                   OR (fund->'state') - ARRAY['allocator'] <> '{}'::jsonb
               )
           )
           OR fund->>'fund_type' NOT IN ('csp', 'covered_call', 'meta_wheel')
           OR (
               fund->>'fund_type' IN ('csp', 'covered_call')
               AND (
                   (fund->'state') - ARRAY['allocator', 'operations'] <> '{}'::jsonb
                   OR jsonb_typeof(fund->'state'->'operations') IS DISTINCT FROM 'object'
                   OR NOT ((fund->'state'->'operations') ?& ARRAY[
                       'latest_block', 'batch_id', 'batch', 'open_batch_id',
                       'nav', 'eligible_supply', 'idle_assets', 'virtual_shares',
                       'max_window_outflow_bps', 'window_eligible_supply',
                       'window_processed_shares'
                   ])
                   OR (fund->'state'->'operations') - ARRAY[
                       'latest_block', 'batch_id', 'batch', 'open_batch_id',
                       'nav', 'eligible_supply', 'idle_assets', 'virtual_shares',
                       'max_window_outflow_bps', 'window_eligible_supply',
                       'window_processed_shares'
                   ] <> '{}'::jsonb
                   OR jsonb_typeof(fund->'state'->'allocator'->'series') IS DISTINCT FROM 'object'
                   OR EXISTS (
                       SELECT 1
                       FROM jsonb_each(fund->'state'->'allocator'->'series') series
                       WHERE NOT (series.value ?& ARRAY[
                           'is_put', 'underlying', 'strike_asset',
                           'collateral_asset', 'expiry', 'strike_price'
                       ])
                          OR series.value - ARRAY[
                              'is_put', 'underlying', 'strike_asset',
                              'collateral_asset', 'expiry', 'strike_price'
                          ] <> '{}'::jsonb
                   )
                   OR (
                       fund->>'fund_type' = 'csp'
                       AND (
                           jsonb_typeof(fund->'state'->'allocator'->'quote_states') IS DISTINCT FROM 'object'
                           OR EXISTS (
                               SELECT 1 FROM jsonb_each(
                                   fund->'state'->'allocator'->'quote_states'
                               ) quote_state
                               WHERE NOT (quote_state.value ?& ARRAY['filled_amount', 'cancelled'])
                                  OR quote_state.value - ARRAY['filled_amount', 'cancelled'] <> '{}'::jsonb
                           )
                       )
                   )
               )
           ) THEN
            RAISE EXCEPTION 'invalid or incomplete snapshot fund payload';
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_funds) f
        WHERE f->>'fund_type' = 'covered_call'
          AND (
              jsonb_typeof(f->'state'->'allocator'->'valuation_observers') IS DISTINCT FROM 'array'
              OR jsonb_array_length(f->'state'->'allocator'->'valuation_observers') <> 2
              OR EXISTS (
                  SELECT 1 FROM jsonb_array_elements(
                      f->'state'->'allocator'->'valuation_observers'
                  ) observer
                  WHERE jsonb_typeof(observer) <> 'boolean'
              )
          )
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_funds) f
        WHERE f->>'fund_type' = 'meta_wheel'
          AND (
              NOT ((f->'state'->'allocator'->'wheel_snapshot') ?& ARRAY[
                  'chain_id', 'parent', 'coordinator', 'safe_block',
                  'safe_block_confirmations', 'safe_block_canonical', 'timestamp',
                  'onchain_policy_hash', 'nav_policy_hash', 'nav_coherent',
                  'nav_fresh', 'transition_balances_reconciled', 'csp_lanes',
                  'call_lanes', 'assignment_lots', 'pending_csp_tranches'
              ])
              OR jsonb_typeof(f->'state'->'allocator'->'wheel_quotes') IS DISTINCT FROM 'array'
              OR EXISTS (
                  SELECT 1 FROM jsonb_array_elements(
                      f->'state'->'allocator'->'wheel_quotes'
                  ) quote
                  WHERE NOT (quote ?& ARRAY[
                      'quote_id', 'is_put', 'strike8', 'expiry', 'created_at',
                      'deadline', 'gross_premium_bps', 'maximum_collateral',
                      'canonical_series', 'delta_bps', 'gross_premium',
                      'net_premium', 'collateral', 'execution_slippage_bps',
                      'open_data', 'lane', 'tranche_id', 'lot_id',
                      'allocation_amount'
                  ])
                     OR quote - ARRAY[
                         'quote_id', 'is_put', 'strike8', 'expiry', 'created_at',
                         'deadline', 'gross_premium_bps', 'maximum_collateral',
                         'canonical_series', 'delta_bps', 'gross_premium',
                         'net_premium', 'collateral', 'execution_slippage_bps',
                         'open_data', 'lane', 'tranche_id', 'lot_id',
                         'allocation_amount'
                     ] <> '{}'::jsonb
              )
          )
    ) THEN
        RAISE EXCEPTION 'invalid covered-call or Meta Wheel nested state';
    END IF;

    SELECT w.publish_deadline INTO claim_deadline
    FROM v2_snapshot_windows w
    WHERE w.environment = p_environment
      AND w.chain_id = p_chain_id
      AND w.window_id = p_window_id
      AND w.claim_token = p_claim_token
      AND w.status = 'claimed'
      AND db_now < w.publish_deadline
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'snapshot claim is not publishable';
    END IF;

    SELECT * INTO current_row
    FROM v2_snapshot_current c
    WHERE c.environment = p_environment AND c.chain_id = p_chain_id
    FOR UPDATE;
    IF FOUND AND (
        p_window_id <= current_row.window_id
        OR p_snapshot_block < current_row.snapshot_block
    ) THEN
        RAISE EXCEPTION 'snapshot window or block would regress';
    END IF;
    db_now := clock_timestamp();
    IF db_now >= claim_deadline
       OR db_now - p_snapshot_block_timestamp > interval '45 seconds'
       OR p_snapshot_block_timestamp > db_now + interval '5 seconds' THEN
        RAISE EXCEPTION 'snapshot deadline or block timestamp is invalid';
    END IF;

    FOR ingestion IN SELECT value FROM jsonb_array_elements(p_ingestions)
    LOOP
        PERFORM v2_ingest_fund_window(
            (ingestion->>'chain_id')::BIGINT,
            lower(ingestion->>'fund_address'),
            ingestion->>'indexer_name',
            (ingestion->>'from_block')::BIGINT,
            (ingestion->>'to_block')::BIGINT,
            lower(ingestion->>'last_block_hash'),
            coalesce(ingestion->'events', '[]'::jsonb),
            ingestion->'projection'
        );
    END LOOP;

    INSERT INTO v2_snapshot_envelopes (
        environment, chain_id, window_id, snapshot_block,
        snapshot_block_hash, snapshot_block_timestamp, published_at,
        reconciled, common, funds
    ) VALUES (
        p_environment, p_chain_id, p_window_id, p_snapshot_block,
        lower(p_snapshot_block_hash), p_snapshot_block_timestamp, db_now,
        TRUE, p_common, p_funds
    )
    RETURNING generation INTO new_generation;

    INSERT INTO v2_snapshot_current (
        environment, chain_id, window_id, generation, snapshot_block
    ) VALUES (
        p_environment, p_chain_id, p_window_id, new_generation, p_snapshot_block
    )
    ON CONFLICT (environment, chain_id) DO UPDATE SET
        window_id = EXCLUDED.window_id,
        generation = EXCLUDED.generation,
        snapshot_block = EXCLUDED.snapshot_block
    WHERE EXCLUDED.window_id > v2_snapshot_current.window_id
      AND EXCLUDED.snapshot_block >= v2_snapshot_current.snapshot_block
    RETURNING TRUE INTO pointer_updated;
    IF NOT coalesce(pointer_updated, FALSE) THEN
        RAISE EXCEPTION 'snapshot lost monotonic current-pointer race';
    END IF;

    INSERT INTO v2_confirmed_chain_heads AS h (
        chain_id, block_number, block_hash, observed_at
    ) VALUES (
        p_chain_id, p_snapshot_block, lower(p_snapshot_block_hash), db_now
    )
    ON CONFLICT (chain_id) DO UPDATE SET
        block_number = EXCLUDED.block_number,
        block_hash = EXCLUDED.block_hash,
        observed_at = EXCLUDED.observed_at
    WHERE EXCLUDED.block_number > h.block_number
       OR (
           EXCLUDED.block_number = h.block_number
           AND EXCLUDED.block_hash = h.block_hash
       )
    RETURNING TRUE INTO head_updated;
    IF NOT coalesce(head_updated, FALSE) THEN
        RAISE EXCEPTION 'snapshot confirmed head would regress or change hash';
    END IF;

    UPDATE v2_fund_state s SET
        share_supply = coalesce(
            (payload_fund->'state'->'operations'->>'eligible_supply')::NUMERIC,
            s.share_supply
        ),
        positions_hash = coalesce(
            payload_fund->'state'->'allocator'->>'strategy_hash', s.positions_hash
        ),
        last_report_nonce = coalesce(
            (payload_fund->'state'->'allocator'->'nav'->>9)::BIGINT,
            s.last_report_nonce
        ),
        accounted_idle_assets = coalesce(
            (payload_fund->'state'->'allocator'->>'idle_assets')::NUMERIC,
            s.accounted_idle_assets
        ),
        virtual_shares = coalesce(
            (payload_fund->'state'->'operations'->>'virtual_shares')::NUMERIC,
            s.virtual_shares
        ),
        has_active_processing = coalesce(
            (payload_fund->'state'->'allocator'->>'processing')::BOOLEAN,
            s.has_active_processing
        ),
        fund_flow_nonce = coalesce(
            (payload_fund->'state'->'allocator'->'nav'->>13)::BIGINT,
            s.fund_flow_nonce
        ),
        idle_state_hash = coalesce(
            payload_fund->'state'->'allocator'->'nav'->>14, s.idle_state_hash
        ),
        as_of_block = p_snapshot_block,
        as_of_block_hash = lower(p_snapshot_block_hash),
        reconciled = TRUE,
        indexed_at = db_now,
        snapshot_generation = new_generation,
        snapshot_published_at = db_now,
        snapshot_state = payload_fund->'state',
        updated_at = db_now
    FROM jsonb_array_elements(p_funds) payload_fund
    WHERE s.chain_id = p_chain_id
      AND s.fund_address = lower(payload_fund->>'fund_address');
    GET DIAGNOSTICS normalized_update_count = ROW_COUNT;
    IF normalized_update_count <> payload_fund_count THEN
        RAISE EXCEPTION 'snapshot did not update every normalized fund row';
    END IF;

    UPDATE v2_snapshot_windows w
    SET status = 'published', published_at = db_now, generation = new_generation
    WHERE w.environment = p_environment
      AND w.chain_id = p_chain_id
      AND w.window_id = p_window_id;

    UPDATE v2_snapshot_collector_control c
    SET failure_streak = 0, circuit_open_until = NULL, updated_at = db_now
    WHERE c.environment = p_environment AND c.chain_id = p_chain_id;

    RETURN new_generation;
END;
$$;

CREATE OR REPLACE FUNCTION v2_get_snapshot_inputs(
    p_chain_id BIGINT
) RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    WITH makers AS (
        SELECT DISTINCT lower(mm_address) AS mm_address
        FROM mm_api_keys
        WHERE is_active
    ), selected_maker AS (
        SELECT mm_address FROM makers ORDER BY mm_address LIMIT 1
    )
    SELECT jsonb_build_object(
        'maker_count', (SELECT count(*) FROM makers),
        'mm_address', (SELECT mm_address FROM selected_maker),
        'quotes', coalesce((
            SELECT jsonb_agg(
                to_jsonb(q) || jsonb_build_object(
                    'deployment_status', o.deployment_status
                ) ORDER BY q.quote_id
            )
            FROM mm_quotes q
            LEFT JOIN available_otokens o
              ON lower(o.otoken_address) = lower(q.otoken_address)
             AND o.chain = q.chain
            WHERE q.mm_address = (SELECT mm_address FROM selected_maker)
              AND q.chain = 'base' AND q.is_active
              AND q.deadline > floor(extract(epoch FROM clock_timestamp()))::BIGINT
        ), '[]'::jsonb),
        'available_otokens', coalesce((
            SELECT jsonb_agg(to_jsonb(o) ORDER BY o.expiry, o.strike_price, o.is_put)
            FROM available_otokens o
            WHERE o.chain = 'base'
              AND (o.chain_id IS NULL OR o.chain_id = p_chain_id)
              AND lower(o.underlying) IN (
                  SELECT DISTINCT r2.weth FROM v2_fund_registry r2
                  WHERE r2.chain_id = p_chain_id AND r2.enabled
              )
              AND o.expiry > floor(extract(epoch FROM clock_timestamp()))::BIGINT
              AND o.deployment_status <> 'failed'
        ), '[]'::jsonb),
        'funds', coalesce((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'registry', to_jsonb(r),
                    'contracts', coalesce((
                        SELECT jsonb_agg(to_jsonb(c) ORDER BY c.contract_role, c.valid_from_block)
                        FROM v2_fund_contracts c
                        WHERE c.chain_id = r.chain_id
                          AND c.fund_address = r.fund_address
                    ), '[]'::jsonb),
                    'state', to_jsonb(s),
                    'checkpoint', (
                        SELECT to_jsonb(cp) FROM v2_indexer_checkpoints cp
                        WHERE cp.chain_id = r.chain_id
                          AND cp.fund_address = r.fund_address
                          AND cp.indexer_name = 'tokenized_csp_fund'
                    ),
                    'positions', coalesce((
                        SELECT jsonb_agg(to_jsonb(p) ORDER BY p.position_id)
                        FROM v2_fund_strategy_positions p
                        WHERE p.chain_id = r.chain_id
                          AND p.fund_address = r.fund_address
                    ), '[]'::jsonb),
                    'inventory', coalesce((
                        SELECT jsonb_agg(to_jsonb(i) ORDER BY i.asset_address, i.bucket)
                        FROM v2_fund_inventory i
                        WHERE i.chain_id = r.chain_id
                          AND i.fund_address = r.fund_address
                    ), '[]'::jsonb),
                    'redemption_batches', coalesce((
                        SELECT jsonb_agg(to_jsonb(b) ORDER BY b.latest_batch_id)
                        FROM v2_redemption_batch_states b
                        WHERE b.chain_id = r.chain_id
                          AND b.fund_address = r.fund_address
                    ), '[]'::jsonb),
                    'meta_state', (
                        SELECT to_jsonb(ms) FROM v2_meta_wheel_state ms
                        WHERE ms.chain_id = r.chain_id
                          AND ms.fund_address = r.fund_address
                    ),
                    'meta_lanes', coalesce((
                        SELECT jsonb_agg(to_jsonb(ml) ORDER BY ml.registration_index)
                        FROM v2_meta_wheel_lanes ml
                        WHERE ml.chain_id = r.chain_id
                          AND ml.fund_address = r.fund_address
                    ), '[]'::jsonb),
                    'meta_tranches', coalesce((
                        SELECT jsonb_agg(to_jsonb(mt) ORDER BY mt.tranche_id)
                        FROM v2_meta_wheel_tranches mt
                        WHERE mt.chain_id = r.chain_id
                          AND mt.fund_address = r.fund_address
                    ), '[]'::jsonb),
                    'meta_lots', coalesce((
                        SELECT jsonb_agg(to_jsonb(l) ORDER BY l.lot_id)
                        FROM v2_meta_wheel_assignment_lots l
                        WHERE l.chain_id = r.chain_id
                          AND l.fund_address = r.fund_address
                    ), '[]'::jsonb),
                    'meta_nav', (
                        SELECT to_jsonb(n) FROM v2_meta_wheel_nav_snapshots n
                        WHERE n.chain_id = r.chain_id
                          AND n.fund_address = r.fund_address
                        ORDER BY n.snapshot_block DESC LIMIT 1
                    ),
                    'meta_lane_valuations', coalesce((
                        SELECT jsonb_agg(to_jsonb(v) ORDER BY v.child_vault)
                        FROM v2_meta_wheel_lane_valuations v
                        WHERE v.chain_id = r.chain_id
                          AND v.fund_address = r.fund_address
                          AND v.snapshot_block = (
                              SELECT max(n2.snapshot_block)
                              FROM v2_meta_wheel_nav_snapshots n2
                              WHERE n2.chain_id = r.chain_id
                                AND n2.fund_address = r.fund_address
                          )
                    ), '[]'::jsonb)
                ) ORDER BY CASE r.strategy_kind
                    WHEN 'csp' THEN 1 WHEN 'covered_call' THEN 2 ELSE 3 END
            )
            FROM v2_fund_registry r
            LEFT JOIN v2_fund_state s
              ON s.chain_id = r.chain_id AND s.fund_address = r.fund_address
            WHERE r.chain_id = p_chain_id AND r.enabled
        ), '[]'::jsonb)
    );
$$;

CREATE OR REPLACE FUNCTION v2_get_current_snapshot(
    p_environment TEXT,
    p_chain_id BIGINT
) RETURNS JSONB
LANGUAGE sql
VOLATILE
SECURITY INVOKER
SET search_path = public
AS $$
    SELECT jsonb_build_object(
        'environment', e.environment,
        'chain_id', e.chain_id,
        'window_id', e.window_id,
        'generation', e.generation,
        'snapshot_block', e.snapshot_block,
        'snapshot_block_hash', e.snapshot_block_hash,
        'snapshot_block_timestamp', floor(extract(epoch FROM e.snapshot_block_timestamp))::BIGINT,
        'published_at', e.published_at,
        'common', e.common,
        'funds', e.funds,
        'reconciled', e.reconciled,
        'chain_data_age_seconds', greatest(0, extract(epoch FROM clock_timestamp() - e.snapshot_block_timestamp)),
        'stale', clock_timestamp() - e.snapshot_block_timestamp > interval '45 seconds'
                 OR NOT e.reconciled
    )
    FROM v2_snapshot_current c
    JOIN v2_snapshot_envelopes e
      ON e.environment = c.environment
     AND e.chain_id = c.chain_id
     AND e.generation = c.generation
    WHERE c.environment = p_environment AND c.chain_id = p_chain_id;
$$;

ALTER TABLE v2_snapshot_collector_control ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_snapshot_windows ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_snapshot_event_backfills ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_snapshot_envelopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2_snapshot_current ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON v2_snapshot_collector_control, v2_snapshot_windows,
    v2_snapshot_event_backfills, v2_snapshot_envelopes, v2_snapshot_current
    FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON v2_snapshot_collector_control, v2_snapshot_windows,
    v2_snapshot_event_backfills, v2_snapshot_current TO service_role;
GRANT SELECT, INSERT ON v2_snapshot_envelopes TO service_role;
GRANT USAGE, SELECT ON SEQUENCE v2_snapshot_generation_seq TO service_role;
REVOKE EXECUTE ON FUNCTION v2_claim_snapshot_window(TEXT, BIGINT),
    v2_reserve_snapshot_retry(TEXT, BIGINT, BIGINT, UUID),
    v2_fail_snapshot_window(TEXT, BIGINT, BIGINT, UUID, TEXT),
    v2_claim_snapshot_event_backfill(TEXT, BIGINT),
    v2_finish_snapshot_event_backfill(TEXT, BIGINT, BIGINT, BIGINT, BOOLEAN),
    v2_rewind_snapshot_event_backfill(BIGINT, JSONB, BIGINT),
    v2_apply_snapshot_event_backfill_chunk(TEXT, BIGINT, BIGINT, BIGINT, JSONB, BOOLEAN),
    v2_publish_snapshot(TEXT, BIGINT, BIGINT, UUID, BIGINT, TEXT, TIMESTAMPTZ, JSONB, JSONB, JSONB),
    v2_get_snapshot_inputs(BIGINT),
    v2_get_current_snapshot(TEXT, BIGINT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION v2_claim_snapshot_window(TEXT, BIGINT),
    v2_reserve_snapshot_retry(TEXT, BIGINT, BIGINT, UUID),
    v2_fail_snapshot_window(TEXT, BIGINT, BIGINT, UUID, TEXT),
    v2_claim_snapshot_event_backfill(TEXT, BIGINT),
    v2_finish_snapshot_event_backfill(TEXT, BIGINT, BIGINT, BIGINT, BOOLEAN),
    v2_rewind_snapshot_event_backfill(BIGINT, JSONB, BIGINT),
    v2_apply_snapshot_event_backfill_chunk(TEXT, BIGINT, BIGINT, BIGINT, JSONB, BOOLEAN),
    v2_publish_snapshot(TEXT, BIGINT, BIGINT, UUID, BIGINT, TEXT, TIMESTAMPTZ, JSONB, JSONB, JSONB),
    v2_get_snapshot_inputs(BIGINT),
    v2_get_current_snapshot(TEXT, BIGINT) TO service_role;
