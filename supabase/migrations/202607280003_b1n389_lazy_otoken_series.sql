-- B1N-389: deterministic virtual oToken series and cross-process creation lease.

ALTER TABLE available_otokens
    ADD COLUMN IF NOT EXISTS chain_id BIGINT,
    ADD COLUMN IF NOT EXISTS series_key TEXT,
    ADD COLUMN IF NOT EXISTS factory_address TEXT,
    ADD COLUMN IF NOT EXISTS strike_asset TEXT,
    ADD COLUMN IF NOT EXISTS strike_price_raw NUMERIC(78, 0),
    ADD COLUMN IF NOT EXISTS deployment_status TEXT NOT NULL DEFAULT 'ready',
    ADD COLUMN IF NOT EXISTS deployment_owner_token UUID,
    ADD COLUMN IF NOT EXISTS deployment_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deployment_tx_hash TEXT,
    ADD COLUMN IF NOT EXISTS deployment_submitted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS creation_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error_code TEXT,
    ADD COLUMN IF NOT EXISTS first_published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS first_filled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

UPDATE available_otokens
SET ready_at = coalesce(ready_at, created_at),
    updated_at = coalesce(updated_at, created_at)
WHERE deployment_status = 'ready';

ALTER TABLE available_otokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE available_otokens FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE available_otokens FROM PUBLIC, anon, authenticated;

ALTER TABLE available_otokens
    DROP CONSTRAINT IF EXISTS available_otokens_deployment_status_check;
ALTER TABLE available_otokens
    ADD CONSTRAINT available_otokens_deployment_status_check
    CHECK (deployment_status IN ('virtual', 'creating', 'ready', 'failed'));

ALTER TABLE available_otokens
    ADD CONSTRAINT available_otokens_canonical_fields_check
    CHECK (
        series_key IS NULL OR (
            chain = 'base'
            AND chain_id IS NOT NULL
            AND factory_address IS NOT NULL
            AND underlying IS NOT NULL
            AND strike_asset IS NOT NULL
            AND collateral_asset IS NOT NULL
            AND strike_price_raw IS NOT NULL
        )
    ) NOT VALID;
ALTER TABLE available_otokens
    ADD CONSTRAINT available_otokens_canonical_collateral_check
    CHECK (
        series_key IS NULL
        OR (
            is_put AND lower(collateral_asset) = lower(strike_asset)
        )
        OR (
            NOT is_put AND lower(collateral_asset) = lower(underlying)
        )
    ) NOT VALID;
ALTER TABLE available_otokens
    ADD CONSTRAINT available_otokens_canonical_strike_check
    CHECK (
        series_key IS NULL
        OR strike_price_raw = strike_price * 100000000
    ) NOT VALID;
ALTER TABLE available_otokens
    ADD CONSTRAINT available_otokens_canonical_expiry_check
    CHECK (
        series_key IS NULL OR mod(expiry, 86400) = 28800
    ) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS available_otokens_chain_series_key_idx
    ON available_otokens (chain, series_key)
    WHERE series_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS available_otokens_lifecycle_idx
    ON available_otokens (chain, deployment_status, expiry);

CREATE TABLE IF NOT EXISTS otoken_materialization_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_key TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    series_key TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    amount_raw NUMERIC(78, 0) NOT NULL CHECK (amount_raw > 0),
    outcome TEXT NOT NULL DEFAULT 'requested' CHECK (
        outcome IN (
            'requested', 'creating', 'ready', 'rate_limited',
            'stale_quote', 'failed'
        )
    ),
    error_code TEXT,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (actor_key, series_key, quote_hash)
);

ALTER TABLE otoken_materialization_intents ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS otoken_materialization_actor_created_idx
    ON otoken_materialization_intents (actor_key, created_at DESC);
CREATE INDEX IF NOT EXISTS otoken_materialization_wallet_created_idx
    ON otoken_materialization_intents (wallet_address, created_at DESC);
CREATE INDEX IF NOT EXISTS otoken_materialization_series_created_idx
    ON otoken_materialization_intents (series_key, created_at DESC);

CREATE OR REPLACE FUNCTION v1_claim_otoken_materialization(
    p_series_key TEXT,
    p_actor_key TEXT,
    p_wallet_address TEXT,
    p_quote_hash TEXT,
    p_amount_raw NUMERIC,
    p_ownership_token UUID,
    p_lease_seconds INTEGER,
    p_hourly_limit INTEGER,
    p_daily_limit INTEGER,
    p_series_hourly_limit INTEGER,
    p_max_attempts INTEGER
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    series_row available_otokens%ROWTYPE;
    existing_intent BOOLEAN;
    existing_outcome TEXT;
    owned BOOLEAN := FALSE;
    rate_limited BOOLEAN := FALSE;
BEGIN
    IF p_series_key IS NULL OR p_actor_key IS NULL OR p_wallet_address IS NULL
       OR p_quote_hash IS NULL OR p_ownership_token IS NULL OR p_amount_raw <= 0
       OR p_lease_seconds NOT BETWEEN 30 AND 900
       OR p_hourly_limit NOT BETWEEN 1 AND 1000
       OR p_daily_limit NOT BETWEEN 1 AND 10000
       OR p_series_hourly_limit NOT BETWEEN 1 AND 10000
       OR p_max_attempts NOT BETWEEN 1 AND 20 THEN
        RAISE EXCEPTION 'Invalid oToken materialization claim';
    END IF;

    -- Serialize independent-process admission checks before counting. The lock
    -- order is fixed so actor/wallet/series limits cannot race or deadlock.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('otoken-actor:' || p_actor_key, 0)
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended('otoken-wallet:' || lower(p_wallet_address), 0)
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended('otoken-series:' || p_series_key, 0)
    );

    SELECT * INTO series_row
    FROM available_otokens
    WHERE series_key = p_series_key AND chain = 'base'
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'owned', FALSE, 'status', 'missing', 'rate_limited', FALSE
        );
    END IF;

    SELECT outcome INTO existing_outcome
    FROM otoken_materialization_intents
    WHERE actor_key = p_actor_key
      AND series_key = p_series_key
      AND quote_hash = p_quote_hash;
    existing_intent := FOUND;

    IF NOT existing_intent OR existing_outcome = 'rate_limited' THEN
        rate_limited := (
            SELECT count(*) >= p_hourly_limit
            FROM otoken_materialization_intents
            WHERE actor_key = p_actor_key
              AND outcome <> 'rate_limited'
              AND created_at >= now() - interval '1 hour'
        ) OR (
            SELECT count(*) >= p_hourly_limit
            FROM otoken_materialization_intents
            WHERE wallet_address = lower(p_wallet_address)
              AND outcome <> 'rate_limited'
              AND created_at >= now() - interval '1 hour'
        ) OR (
            SELECT count(*) >= p_daily_limit
            FROM otoken_materialization_intents
            WHERE actor_key = p_actor_key
              AND outcome <> 'rate_limited'
              AND created_at >= now() - interval '1 day'
        ) OR (
            SELECT count(*) >= p_daily_limit
            FROM otoken_materialization_intents
            WHERE wallet_address = lower(p_wallet_address)
              AND outcome <> 'rate_limited'
              AND created_at >= now() - interval '1 day'
        ) OR (
            SELECT count(*) >= p_series_hourly_limit
            FROM otoken_materialization_intents
            WHERE series_key = p_series_key
              AND outcome <> 'rate_limited'
              AND created_at >= now() - interval '1 hour'
        );
    END IF;

    INSERT INTO otoken_materialization_intents (
        actor_key, wallet_address, series_key, quote_hash, amount_raw, outcome
    ) VALUES (
        p_actor_key, lower(p_wallet_address), p_series_key, p_quote_hash,
        p_amount_raw, CASE WHEN rate_limited THEN 'rate_limited' ELSE 'requested' END
    )
    ON CONFLICT (actor_key, series_key, quote_hash) DO UPDATE SET
        outcome = CASE
            WHEN rate_limited THEN 'rate_limited'
            WHEN otoken_materialization_intents.outcome = 'rate_limited'
                THEN 'requested'
            ELSE otoken_materialization_intents.outcome
        END,
        updated_at = now();

    IF rate_limited AND series_row.deployment_status != 'ready' THEN
        RETURN jsonb_build_object(
            'owned', FALSE,
            'status', series_row.deployment_status,
            'rate_limited', TRUE,
            'tx_hash', series_row.deployment_tx_hash
        );
    END IF;

    IF series_row.deployment_status = 'ready' THEN
        UPDATE otoken_materialization_intents
        SET outcome = 'ready', updated_at = now()
        WHERE actor_key = p_actor_key
          AND series_key = p_series_key
          AND quote_hash = p_quote_hash;
    ELSIF series_row.creation_attempts >= p_max_attempts THEN
        RETURN jsonb_build_object(
            'owned', FALSE,
            'status', series_row.deployment_status,
            'rate_limited', FALSE,
            'attempts_exhausted', TRUE,
            'tx_hash', series_row.deployment_tx_hash
        );
    ELSIF series_row.deployment_status IN ('virtual', 'failed')
       OR (
           series_row.deployment_status = 'creating'
           AND coalesce(series_row.deployment_lease_expires_at, '-infinity') <= now()
       ) THEN
        UPDATE available_otokens SET
            deployment_status = 'creating',
            deployment_owner_token = p_ownership_token,
            deployment_lease_expires_at =
                now() + make_interval(secs => p_lease_seconds),
            creation_attempts = creation_attempts + 1,
            last_error_code = NULL,
            updated_at = now()
        WHERE id = series_row.id;
        owned := TRUE;
        series_row.deployment_status := 'creating';
        UPDATE otoken_materialization_intents
        SET outcome = 'creating', updated_at = now()
        WHERE actor_key = p_actor_key
          AND series_key = p_series_key
          AND quote_hash = p_quote_hash;
    END IF;

    RETURN jsonb_build_object(
        'owned', owned,
        'status', series_row.deployment_status,
        'rate_limited', FALSE,
        'attempts_exhausted', FALSE,
        'tx_hash', series_row.deployment_tx_hash
    );
END;
$$;

CREATE OR REPLACE FUNCTION v1_record_otoken_materialization_broadcast(
    p_series_key TEXT,
    p_ownership_token UUID,
    p_transaction_hash TEXT,
    p_lease_seconds INTEGER
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_series_key IS NULL OR p_ownership_token IS NULL
       OR p_transaction_hash IS NULL
       OR p_transaction_hash !~ '^0x[0-9a-fA-F]{64}$'
       OR p_lease_seconds NOT BETWEEN 30 AND 900 THEN
        RAISE EXCEPTION 'Invalid oToken broadcast';
    END IF;
    UPDATE available_otokens SET
        deployment_tx_hash = lower(p_transaction_hash),
        deployment_submitted_at = coalesce(deployment_submitted_at, now()),
        deployment_lease_expires_at =
            now() + make_interval(secs => p_lease_seconds),
        updated_at = now()
    WHERE series_key = p_series_key
      AND chain = 'base'
      AND deployment_status = 'creating'
      AND deployment_owner_token = p_ownership_token
      AND (
          deployment_tx_hash IS NULL
          OR lower(deployment_tx_hash) = lower(p_transaction_hash)
      );
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE OR REPLACE FUNCTION v1_complete_otoken_materialization(
    p_series_key TEXT,
    p_ownership_token UUID,
    p_transaction_hash TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE available_otokens SET
        deployment_status = 'ready',
        deployment_tx_hash = coalesce(p_transaction_hash, deployment_tx_hash),
        deployment_owner_token = NULL,
        deployment_lease_expires_at = NULL,
        last_error_code = NULL,
        ready_at = coalesce(ready_at, now()),
        updated_at = now()
    WHERE series_key = p_series_key
      AND chain = 'base'
      AND deployment_status = 'creating'
      AND deployment_owner_token = p_ownership_token
      AND deployment_lease_expires_at > now();
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE OR REPLACE FUNCTION v1_reconcile_ready_otoken(
    p_series_key TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_series_key IS NULL THEN
        RAISE EXCEPTION 'Invalid oToken reconciliation';
    END IF;
    UPDATE available_otokens SET
        deployment_status = 'ready',
        deployment_owner_token = NULL,
        deployment_lease_expires_at = NULL,
        last_error_code = NULL,
        ready_at = coalesce(ready_at, now()),
        updated_at = now()
    WHERE series_key = p_series_key
      AND chain = 'base';
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE OR REPLACE FUNCTION v1_fail_otoken_materialization(
    p_series_key TEXT,
    p_ownership_token UUID,
    p_error_code TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE available_otokens SET
        deployment_status = 'failed',
        deployment_tx_hash = NULL,
        deployment_submitted_at = NULL,
        deployment_owner_token = NULL,
        deployment_lease_expires_at = NULL,
        last_error_code = left(p_error_code, 80),
        updated_at = now()
    WHERE series_key = p_series_key
      AND chain = 'base'
      AND deployment_status = 'creating'
      AND deployment_owner_token = p_ownership_token;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE OR REPLACE FUNCTION v1_record_otoken_intent_outcome(
    p_actor_key TEXT,
    p_series_key TEXT,
    p_quote_hash TEXT,
    p_outcome TEXT,
    p_error_code TEXT,
    p_latency_ms INTEGER
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_outcome NOT IN ('requested', 'creating', 'ready', 'stale_quote', 'failed') THEN
        RAISE EXCEPTION 'Invalid oToken intent outcome';
    END IF;
    UPDATE otoken_materialization_intents SET
        outcome = p_outcome,
        error_code = left(p_error_code, 80),
        latency_ms = p_latency_ms,
        updated_at = now()
    WHERE actor_key = p_actor_key
      AND series_key = p_series_key
      AND quote_hash = p_quote_hash;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

REVOKE ALL ON FUNCTION v1_claim_otoken_materialization(
    TEXT, TEXT, TEXT, TEXT, NUMERIC, UUID, INTEGER, INTEGER, INTEGER, INTEGER,
    INTEGER
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION v1_record_otoken_materialization_broadcast(
    TEXT, UUID, TEXT, INTEGER
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION v1_complete_otoken_materialization(
    TEXT, UUID, TEXT
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION v1_reconcile_ready_otoken(
    TEXT
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION v1_fail_otoken_materialization(
    TEXT, UUID, TEXT
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION v1_record_otoken_intent_outcome(
    TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER
) FROM PUBLIC, anon, authenticated;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT ALL ON available_otokens TO service_role;
        GRANT ALL ON otoken_materialization_intents TO service_role;
        GRANT EXECUTE ON FUNCTION v1_claim_otoken_materialization(
            TEXT, TEXT, TEXT, TEXT, NUMERIC, UUID, INTEGER, INTEGER, INTEGER,
            INTEGER, INTEGER
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v1_record_otoken_materialization_broadcast(
            TEXT, UUID, TEXT, INTEGER
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v1_complete_otoken_materialization(
            TEXT, UUID, TEXT
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v1_reconcile_ready_otoken(
            TEXT
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v1_fail_otoken_materialization(
            TEXT, UUID, TEXT
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION v1_record_otoken_intent_outcome(
            TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER
        ) TO service_role;
    END IF;
END;
$access$;
