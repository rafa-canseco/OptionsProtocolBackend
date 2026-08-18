-- B1N-434: bounded, atomic legacy weekly aggregation.
-- Reuse migration 004's idx_order_events_indexed_user_id source-window index.

CREATE SCHEMA IF NOT EXISTS private;

-- Match Python round(binary64, ndigits) at the two precisions used by the
-- retired weekly materialization. Calculations stay NUMERIC until each legacy
-- wire/storage rounding point, then pass through the exact binary64 value.
CREATE OR REPLACE FUNCTION private.b1n434_python_round(
    p_value DOUBLE PRECISION,
    p_ndigits INTEGER
)
RETURNS NUMERIC
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    payload BYTEA := float8send(abs(p_value));
    exponent_bits INTEGER;
    mantissa NUMERIC;
    binary_exponent INTEGER;
    scaled_numerator NUMERIC;
    divisor NUMERIC := 1;
    rounded_integer NUMERIC;
    remainder NUMERIC;
    byte_index INTEGER;
BEGIN
    IF p_ndigits NOT IN (2, 4) THEN
        RAISE EXCEPTION 'ndigits must be 2 or 4';
    END IF;

    exponent_bits := ((get_byte(payload, 0) & 127) << 4)
        | ((get_byte(payload, 1) & 240) >> 4);
    IF exponent_bits = 2047 THEN
        RAISE EXCEPTION 'value must be finite';
    END IF;

    mantissa := (get_byte(payload, 1) & 15)::NUMERIC;
    FOR byte_index IN 2..7 LOOP
        mantissa := mantissa * 256 + get_byte(payload, byte_index);
    END LOOP;

    IF exponent_bits = 0 THEN
        binary_exponent := -1074;
    ELSE
        mantissa := mantissa + power(2::NUMERIC, 52);
        binary_exponent := exponent_bits - 1023 - 52;
    END IF;

    scaled_numerator := mantissa * power(5::NUMERIC, p_ndigits);
    binary_exponent := binary_exponent + p_ndigits;
    IF binary_exponent >= 0 THEN
        scaled_numerator := scaled_numerator * power(2::NUMERIC, binary_exponent);
    ELSE
        divisor := power(2::NUMERIC, -binary_exponent);
    END IF;

    rounded_integer := div(scaled_numerator, divisor);
    remainder := scaled_numerator - rounded_integer * divisor;
    IF remainder * 2 > divisor
        OR (remainder * 2 = divisor AND mod(rounded_integer, 2) = 1)
    THEN
        rounded_integer := rounded_integer + 1;
    END IF;

    IF p_value < 0 THEN
        rounded_integer := -rounded_integer;
    END IF;
    RETURN rounded_integer / power(10::NUMERIC, p_ndigits);
END;
$$;

-- Force PostgreSQL to use the same ordered binary64 transition as Python's
-- ``total += value``. An INITCOND of zero is material: the predecessor starts
-- every premium/report accumulation at 0.0 rather than performing a NUMERIC
-- sum and converting only the final exact total to binary64.
CREATE OR REPLACE FUNCTION private.b1n434_float_add(
    p_state DOUBLE PRECISION,
    p_value DOUBLE PRECISION
)
RETURNS DOUBLE PRECISION
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_state + p_value
$function$;

CREATE AGGREGATE private.b1n434_ordered_float_sum(DOUBLE PRECISION) (
    SFUNC = private.b1n434_float_add,
    STYPE = DOUBLE PRECISION,
    INITCOND = '0',
    PARALLEL = UNSAFE
);

CREATE OR REPLACE FUNCTION private.b1n434_validate_window(
    p_week_start TIMESTAMPTZ,
    p_week_end TIMESTAMPTZ
)
RETURNS VOID
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    utc_start TIMESTAMP := p_week_start AT TIME ZONE 'UTC';
    utc_end TIMESTAMP := p_week_end AT TIME ZONE 'UTC';
BEGIN
    IF p_week_end <> p_week_start + INTERVAL '7 days'
        OR extract(isodow FROM utc_start) <> 5
        OR extract(isodow FROM utc_end) <> 5
        OR utc_start::TIME <> TIME '08:00:00'
        OR utc_end::TIME <> TIME '08:00:00'
    THEN
        RAISE EXCEPTION 'window must be exactly Friday 08:00 UTC to Friday 08:00 UTC';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_legacy_week_source(
    p_week_start TIMESTAMPTZ,
    p_week_end TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    source_rows BIGINT;
    source_max_indexed_at TIMESTAMPTZ;
    source_max_id UUID;
BEGIN
    IF p_week_start IS NULL OR p_week_end IS NULL THEN
        RAISE EXCEPTION 'week boundaries are required';
    END IF;
    PERFORM private.b1n434_validate_window(p_week_start, p_week_end);

    SELECT count(*), max(event.indexed_at)
    INTO source_rows, source_max_indexed_at
    FROM public.order_events AS event
    WHERE event.indexed_at >= p_week_start
      AND event.indexed_at < p_week_end;

    SELECT event.id
    INTO source_max_id
    FROM public.order_events AS event
    WHERE event.indexed_at >= p_week_start
      AND event.indexed_at < p_week_end
    ORDER BY event.indexed_at DESC, event.user_address DESC, event.id DESC
    LIMIT 1;

    RETURN jsonb_build_object(
        'has_rows', source_rows > 0,
        'source_rows', source_rows,
        'week_start', to_char(p_week_start AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
        'week_end', to_char(p_week_end AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
        'source_max_indexed_at', source_max_indexed_at,
        'source_max_id', source_max_id
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_aggregate_legacy_week(
    p_week_start TIMESTAMPTZ,
    p_week_end TIMESTAMPTZ,
    p_eth_open DOUBLE PRECISION,
    p_eth_close DOUBLE PRECISION,
    p_eth_high DOUBLE PRECISION,
    p_eth_low DOUBLE PRECISION
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    week_start_text TEXT;
    week_end_text TEXT;
    result JSONB;
BEGIN
    IF p_week_start IS NULL OR p_week_end IS NULL THEN
        RAISE EXCEPTION 'week boundaries are required';
    END IF;
    PERFORM private.b1n434_validate_window(p_week_start, p_week_end);

    IF p_eth_open IS NULL OR p_eth_close IS NULL
        OR p_eth_high IS NULL OR p_eth_low IS NULL
        OR p_eth_open::TEXT IN ('NaN', 'Infinity', '-Infinity')
        OR p_eth_close::TEXT IN ('NaN', 'Infinity', '-Infinity')
        OR p_eth_high::TEXT IN ('NaN', 'Infinity', '-Infinity')
        OR p_eth_low::TEXT IN ('NaN', 'Infinity', '-Infinity')
        OR p_eth_open <= 0 OR p_eth_close <= 0
        OR p_eth_high <= 0 OR p_eth_low <= 0
        OR p_eth_low > least(p_eth_open, p_eth_close)
        OR p_eth_high < greatest(p_eth_open, p_eth_close)
    THEN
        RAISE EXCEPTION 'OHLC must be finite, positive, and coherent';
    END IF;

    week_start_text := to_char(p_week_start AT TIME ZONE 'UTC', 'YYYY-MM-DD');
    week_end_text := to_char(p_week_end AT TIME ZONE 'UTC', 'YYYY-MM-DD');

    -- Serialize retries and competing workers for this exact weekly snapshot.
    -- Never derive the key from TIMESTAMPTZ::TEXT: that rendering changes
    -- with the session TimeZone and would let two workers miss each other. The
    -- validated UTC date is the canonical identity of this Friday-08:00 window.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'b1n434:' || week_start_text,
        434
    ));

    -- Replacing an older snapshot alone would leave the stored cumulative
    -- suffix inconsistent. A future rebuild ticket must update that suffix as
    -- one operation instead of bypassing this guard.
    IF EXISTS (
        SELECT 1
        FROM public.user_weekly_results AS later
        WHERE later.week_start > week_start_text
    ) OR EXISTS (
        SELECT 1
        FROM public.weekly_reports AS later
        WHERE later.week_start > week_start_text
    ) THEN
        RAISE EXCEPTION 'unsafe historical recomputation: later weekly snapshots exist';
    END IF;

    -- The predecessor did not request an order from PostgREST. We now pin the
    -- source sequence to (indexed_at, normalized wallet, id), matching the
    -- existing source-window index's natural ascending traversal while making
    -- compatibility reproducible. This necessarily makes formerly unspecified
    -- tie ordering explicit. For each wallet, Python first added every premium
    -- in source order, then subtracted positive assignment losses in that same
    -- order. Source monetary columns are NUMERIC, so malformed stored strings
    -- cannot exist; NULL and zero fallback behavior is preserved below.
    WITH source AS MATERIALIZED (
        SELECT
            event.id,
            lower(coalesce(event.user_address, '')) AS wallet,
            CASE
                WHEN coalesce(nullif(event.net_premium, 0), event.premium) IS NULL
                THEN NULL
                ELSE coalesce(nullif(event.net_premium, 0), event.premium)
                    ::DOUBLE PRECISION / 1000000::DOUBLE PRECISION
            END AS premium_value,
            event.is_settled,
            event.is_itm,
            event.strike_price,
            event.amount,
            event.is_put,
            event.indexed_at
        FROM public.order_events AS event
        WHERE event.indexed_at >= p_week_start
          AND event.indexed_at < p_week_end
    ),
    event_values AS MATERIALIZED (
        SELECT
            source.*,
            CASE
                WHEN source.is_settled IS TRUE
                    AND source.is_itm IS TRUE
                    AND source.strike_price IS NOT NULL
                    AND source.amount IS NOT NULL
                    AND source.is_put IS TRUE
                THEN (
                    source.strike_price::DOUBLE PRECISION
                        / 100000000::DOUBLE PRECISION - p_eth_close
                ) * (
                    source.amount::DOUBLE PRECISION
                        / 100000000::DOUBLE PRECISION
                )
                WHEN source.is_settled IS TRUE
                    AND source.is_itm IS TRUE
                    AND source.strike_price IS NOT NULL
                    AND source.amount IS NOT NULL
                    AND source.is_put IS NOT TRUE
                THEN (
                    p_eth_close - source.strike_price::DOUBLE PRECISION
                        / 100000000::DOUBLE PRECISION
                ) * (
                    source.amount::DOUBLE PRECISION
                        / 100000000::DOUBLE PRECISION
                )
                ELSE NULL
            END AS loss_value
        FROM source
    ),
    wallet_base AS MATERIALIZED (
        SELECT
            event_values.wallet,
            count(*)::INTEGER AS positions_opened,
            private.b1n434_ordered_float_sum(
                event_values.premium_value
                ORDER BY event_values.indexed_at, event_values.id
            ) FILTER (WHERE event_values.premium_value IS NOT NULL) AS raw_premium,
            count(*) FILTER (
                WHERE event_values.is_settled IS TRUE
                  AND event_values.is_itm IS TRUE
            )::INTEGER AS assignments,
            min(event_values.indexed_at) AS first_indexed_at
        FROM event_values
        WHERE event_values.wallet <> ''
        GROUP BY event_values.wallet
    ),
    wallet_transitions AS MATERIALIZED (
        SELECT
            event_values.wallet,
            0 AS phase,
            event_values.indexed_at,
            event_values.id,
            event_values.premium_value AS delta
        FROM event_values
        WHERE event_values.wallet <> ''
          AND event_values.premium_value IS NOT NULL
        UNION ALL
        SELECT
            event_values.wallet,
            1 AS phase,
            event_values.indexed_at,
            event_values.id,
            -event_values.loss_value AS delta
        FROM event_values
        WHERE event_values.wallet <> ''
          AND event_values.loss_value > 0::DOUBLE PRECISION
    ),
    wallet_float AS MATERIALIZED (
        SELECT
            wallet_transitions.wallet,
            private.b1n434_ordered_float_sum(
                wallet_transitions.delta
                ORDER BY wallet_transitions.phase,
                    wallet_transitions.indexed_at,
                    wallet_transitions.id
            ) AS raw_pnl
        FROM wallet_transitions
        GROUP BY wallet_transitions.wallet
    ),
    wallet_week AS MATERIALIZED (
        SELECT
            wallet_base.wallet,
            wallet_base.positions_opened,
            coalesce(wallet_base.raw_premium, 0::DOUBLE PRECISION) AS raw_premium,
            wallet_base.assignments,
            coalesce(wallet_float.raw_pnl, 0::DOUBLE PRECISION) AS raw_pnl,
            wallet_base.first_indexed_at
        FROM wallet_base
        LEFT JOIN wallet_float USING (wallet)
    ),
    wallet_values AS MATERIALIZED (
        SELECT
            wallet_week.wallet,
            wallet_week.positions_opened,
            private.b1n434_python_round(wallet_week.raw_premium, 4)
                AS rounded_premium,
            wallet_week.assignments,
            private.b1n434_python_round(wallet_week.raw_pnl, 4) AS rounded_pnl,
            private.b1n434_python_round(
                coalesce((
                    SELECT previous.cumulative_pnl
                    FROM public.user_weekly_results AS previous
                    WHERE previous.user_address = wallet_week.wallet
                      AND previous.week_start < week_start_text
                    ORDER BY previous.week_start DESC
                    LIMIT 1
                ), 0)::DOUBLE PRECISION + wallet_week.raw_pnl,
                4
            ) AS rounded_cumulative,
            wallet_week.first_indexed_at
        FROM wallet_week
    ),
    deleted_stale AS (
        DELETE FROM public.user_weekly_results AS stale
        WHERE stale.week_start = week_start_text
          AND NOT EXISTS (
              SELECT 1
              FROM wallet_values
              WHERE wallet_values.wallet = stale.user_address
          )
    ),
    written_users AS (
        INSERT INTO public.user_weekly_results (
            user_address,
            week_start,
            week_end,
            positions_opened,
            total_simulated_premium,
            assignments,
            simulated_pnl,
            cumulative_pnl
        )
        SELECT
            wallet_values.wallet,
            week_start_text,
            week_end_text,
            wallet_values.positions_opened,
            wallet_values.rounded_premium,
            wallet_values.assignments,
            wallet_values.rounded_pnl,
            wallet_values.rounded_cumulative
        FROM wallet_values
        ON CONFLICT (user_address, week_start) DO UPDATE SET
            week_end = EXCLUDED.week_end,
            positions_opened = EXCLUDED.positions_opened,
            total_simulated_premium = EXCLUDED.total_simulated_premium,
            assignments = EXCLUDED.assignments,
            simulated_pnl = EXCLUDED.simulated_pnl,
            cumulative_pnl = EXCLUDED.cumulative_pnl,
            updated_at = now()
        WHERE (
            user_weekly_results.week_end,
            user_weekly_results.positions_opened,
            user_weekly_results.total_simulated_premium,
            user_weekly_results.assignments,
            user_weekly_results.simulated_pnl,
            user_weekly_results.cumulative_pnl
        ) IS DISTINCT FROM (
            EXCLUDED.week_end,
            EXCLUDED.positions_opened,
            EXCLUDED.total_simulated_premium,
            EXCLUDED.assignments,
            EXCLUDED.simulated_pnl,
            EXCLUDED.cumulative_pnl
        )
    ),
    report_values AS MATERIALIZED (
        SELECT
            count(*)::INTEGER AS total_users,
            (SELECT count(*)::INTEGER FROM source) AS total_positions,
            private.b1n434_python_round(
                private.b1n434_ordered_float_sum(
                    wallet_values.rounded_premium::DOUBLE PRECISION
                    ORDER BY wallet_values.first_indexed_at, wallet_values.wallet
                ),
                4
            ) AS total_premium,
            coalesce(sum(wallet_values.assignments), 0)::INTEGER AS total_assignments,
            CASE
                WHEN count(*) = 0 THEN '{}'::JSONB
                ELSE jsonb_build_object(
                    'highest_premium_earned', max(wallet_values.rounded_premium),
                    'most_active_positions', max(wallet_values.positions_opened),
                    'total_unique_users', count(*)::INTEGER,
                    'eth_week_change_pct', private.b1n434_python_round(
                        ((p_eth_close - p_eth_open) / p_eth_open) * 100,
                        2
                    ),
                    'users_with_assignments', count(*) FILTER (
                        WHERE wallet_values.assignments > 0
                    )::INTEGER
                )
            END AS narrative_data
        FROM wallet_values
    ),
    written_report AS (
        INSERT INTO public.weekly_reports (
            week_start,
            week_end,
            total_users,
            total_positions,
            total_simulated_premium,
            total_assignments,
            eth_open,
            eth_close,
            eth_high,
            eth_low,
            narrative_data
        )
        SELECT
            week_start_text,
            week_end_text,
            report_values.total_users,
            report_values.total_positions,
            report_values.total_premium,
            report_values.total_assignments,
            private.b1n434_python_round(p_eth_open, 2),
            private.b1n434_python_round(p_eth_close, 2),
            private.b1n434_python_round(p_eth_high, 2),
            private.b1n434_python_round(p_eth_low, 2),
            report_values.narrative_data
        FROM report_values
        ON CONFLICT (week_start) DO UPDATE SET
            week_end = EXCLUDED.week_end,
            total_users = EXCLUDED.total_users,
            total_positions = EXCLUDED.total_positions,
            total_simulated_premium = EXCLUDED.total_simulated_premium,
            total_assignments = EXCLUDED.total_assignments,
            eth_open = EXCLUDED.eth_open,
            eth_close = EXCLUDED.eth_close,
            eth_high = EXCLUDED.eth_high,
            eth_low = EXCLUDED.eth_low,
            narrative_data = EXCLUDED.narrative_data,
            updated_at = now()
        WHERE (
            weekly_reports.week_end,
            weekly_reports.total_users,
            weekly_reports.total_positions,
            weekly_reports.total_simulated_premium,
            weekly_reports.total_assignments,
            weekly_reports.eth_open,
            weekly_reports.eth_close,
            weekly_reports.eth_high,
            weekly_reports.eth_low,
            weekly_reports.narrative_data
        ) IS DISTINCT FROM (
            EXCLUDED.week_end,
            EXCLUDED.total_users,
            EXCLUDED.total_positions,
            EXCLUDED.total_simulated_premium,
            EXCLUDED.total_assignments,
            EXCLUDED.eth_open,
            EXCLUDED.eth_close,
            EXCLUDED.eth_high,
            EXCLUDED.eth_low,
            EXCLUDED.narrative_data
        )
    ),
    watermark AS (
        SELECT source.id, source.indexed_at
        FROM source
        ORDER BY source.indexed_at DESC, source.wallet DESC, source.id DESC
        LIMIT 1
    )
    -- The response is derived from calculation CTEs, never INSERT/UPDATE
    -- RETURNING rows, so a no-op conflict retry returns the same fixed object.
    SELECT jsonb_build_object(
        'status', 'aggregated',
        'week_start', week_start_text,
        'week_end', week_end_text,
        'source_rows', report_values.total_positions,
        'wallet_rows', report_values.total_users,
        'assignments', report_values.total_assignments,
        'source_max_indexed_at', watermark.indexed_at,
        'source_max_id', watermark.id
    )
    INTO result
    FROM report_values
    LEFT JOIN watermark ON TRUE;

    RETURN result;
END;
$$;

REVOKE ALL ON SCHEMA private FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION private.b1n434_python_round(DOUBLE PRECISION, INTEGER)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION private.b1n434_float_add(
    DOUBLE PRECISION, DOUBLE PRECISION
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION private.b1n434_ordered_float_sum(DOUBLE PRECISION)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION private.b1n434_validate_window(TIMESTAMPTZ, TIMESTAMPTZ)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_legacy_week_source(TIMESTAMPTZ, TIMESTAMPTZ)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_aggregate_legacy_week(
    TIMESTAMPTZ, TIMESTAMPTZ,
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION
) FROM PUBLIC, anon, authenticated;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT USAGE ON SCHEMA private TO service_role;
        GRANT EXECUTE ON FUNCTION private.b1n434_python_round(
            DOUBLE PRECISION, INTEGER
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION private.b1n434_float_add(
            DOUBLE PRECISION, DOUBLE PRECISION
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION private.b1n434_ordered_float_sum(
            DOUBLE PRECISION
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION private.b1n434_validate_window(
            TIMESTAMPTZ, TIMESTAMPTZ
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION public.b1nary_legacy_week_source(
            TIMESTAMPTZ, TIMESTAMPTZ
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION public.b1nary_aggregate_legacy_week(
            TIMESTAMPTZ, TIMESTAMPTZ,
            DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION
        ) TO service_role;
    END IF;
END;
$access$;

NOTIFY pgrst, 'reload schema';
