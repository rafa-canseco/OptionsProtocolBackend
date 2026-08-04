-- B1N-431: exact, bounded legacy leaderboard aggregates.
-- The generic source-window index is intentionally shared with B1N-434.

CREATE INDEX IF NOT EXISTS idx_order_events_indexed_user_id
    ON public.order_events (indexed_at, user_address, id);

CREATE INDEX IF NOT EXISTS idx_b1n431_order_events_user_indexed_id
    ON public.order_events (user_address, indexed_at, id);

CREATE SCHEMA IF NOT EXISTS private;

-- Python round(float, ndigits) rounds the exact binary64 input to a decimal
-- quantum with ties to even. PostgreSQL's NUMERIC round instead breaks ties
-- away from zero, and scaling a float first can introduce a second rounding.
-- Decode the network-order IEEE-754 payload and round its exact rational value.
-- The leaderboard only needs the two predecessor precisions, so keep the helper
-- deliberately bounded and return the exact decimal quantum used for ordering.
CREATE OR REPLACE FUNCTION private.b1n431_python_round(
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
    IF p_ndigits NOT IN (4, 6) THEN
        RAISE EXCEPTION 'ndigits must be 4 or 6';
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

    -- x * 10^n = mantissa * 5^n * 2^(binary_exponent + n).
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

CREATE OR REPLACE FUNCTION private.b1n431_wallet_stats(p_rows JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    event_row RECORD;
    follow_row RECORD;
    used_ids UUID[] := ARRAY[]::UUID[];
    wheel_ids UUID[] := ARRAY[]::UUID[];
    total_collateral DOUBLE PRECISION := 0;
    adjusted_premium DOUBLE PRECISION := 0;
    event_premium DOUBLE PRECISION;
    active_days INTEGER := 0;
    wheel_count INTEGER := 0;
    position_count INTEGER := 0;
    current_streak INTEGER := 0;
    maximum_streak INTEGER := 0;
    week1_has_itm BOOLEAN := FALSE;
    week2_has_itm BOOLEAN := FALSE;
    earns_bonus BOOLEAN;
BEGIN
    IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
        RAISE EXCEPTION 'rows must be a JSON array';
    END IF;

    position_count := jsonb_array_length(p_rows);

    SELECT coalesce(
        sum(coalesce((item.value ->> 'collateral_usd')::DOUBLE PRECISION, 0)),
        0
    )
    INTO total_collateral
    FROM jsonb_array_elements(p_rows) AS item(value);

    -- Preserve the old inclusive UTC day semantics without expanding one row
    -- per active day. PostgreSQL canonicalizes and merges the date ranges.
    SELECT coalesce(sum(upper(merged.span) - lower(merged.span)), 0)::INTEGER
    INTO active_days
    FROM (
        SELECT unnest(range_agg(daterange(bounds.indexed_day, bounds.expiry_day, '[]'))) AS span
        FROM (
            SELECT
                ((item.value ->> 'indexed_at')::TIMESTAMPTZ AT TIME ZONE 'UTC')::DATE
                    AS indexed_day,
                CASE
                    WHEN item.value ->> 'expiry' IS NULL THEN
                        ((item.value ->> 'indexed_at')::TIMESTAMPTZ AT TIME ZONE 'UTC')::DATE
                    ELSE
                        (to_timestamp((item.value ->> 'expiry')::BIGINT)
                            AT TIME ZONE 'UTC')::DATE
                END AS expiry_day
            FROM jsonb_array_elements(p_rows) AS item(value)
        ) AS bounds
        WHERE bounds.expiry_day >= bounds.indexed_day
    ) AS merged;

    SELECT
        coalesce(bool_or(
            (item.value ->> 'is_itm')::BOOLEAN IS TRUE
            AND (item.value ->> 'settled_at')::TIMESTAMPTZ
                BETWEEN TIMESTAMPTZ '2026-03-30 00:00:00+00'
                    AND TIMESTAMPTZ '2026-04-05 23:59:59+00'
        ), FALSE),
        coalesce(bool_or(
            (item.value ->> 'is_itm')::BOOLEAN IS TRUE
            AND (item.value ->> 'settled_at')::TIMESTAMPTZ
                BETWEEN TIMESTAMPTZ '2026-04-06 00:00:00+00'
                    AND TIMESTAMPTZ '2026-04-12 23:59:59+00'
        ), FALSE)
    INTO week1_has_itm, week2_has_itm
    FROM jsonb_array_elements(p_rows) AS item(value);

    -- Deterministic form of the legacy greedy Wheel pairing: chronological
    -- source order, same asset, opposite sides, both ITM-settled, inclusive
    -- 24-hour follow-up window, and every id consumed at most once.
    FOR event_row IN
        SELECT
            (item.value ->> 'id')::UUID AS id,
            item.value ->> 'asset' AS asset,
            (item.value ->> 'is_put')::BOOLEAN AS is_put,
            (item.value ->> 'settled_at')::TIMESTAMPTZ AS settled_at,
            item.ordinality
        FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS item(value, ordinality)
        WHERE (item.value ->> 'is_itm')::BOOLEAN IS TRUE
          AND item.value ->> 'settled_at' IS NOT NULL
          AND item.value ->> 'is_put' IS NOT NULL
        ORDER BY item.ordinality
    LOOP
        IF event_row.id = ANY(used_ids) THEN
            CONTINUE;
        END IF;

        FOR follow_row IN
            SELECT
                (item.value ->> 'id')::UUID AS id,
                item.value ->> 'asset' AS asset,
                (item.value ->> 'is_put')::BOOLEAN AS is_put,
                (item.value ->> 'indexed_at')::TIMESTAMPTZ AS indexed_at,
                item.ordinality
            FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS item(value, ordinality)
            WHERE (item.value ->> 'is_itm')::BOOLEAN IS TRUE
              AND item.value ->> 'settled_at' IS NOT NULL
              AND item.value ->> 'is_put' IS NOT NULL
              AND (item.value ->> 'id')::UUID <> event_row.id
              AND NOT ((item.value ->> 'id')::UUID = ANY(used_ids))
              AND (item.value ->> 'asset') IS NOT DISTINCT FROM event_row.asset
              AND (item.value ->> 'is_put')::BOOLEAN <> event_row.is_put
              AND (item.value ->> 'indexed_at')::TIMESTAMPTZ
                    BETWEEN event_row.settled_at
                        AND event_row.settled_at + INTERVAL '24 hours'
            ORDER BY item.ordinality
            LIMIT 1
        LOOP
            used_ids := array_append(array_append(used_ids, event_row.id), follow_row.id);
            wheel_ids := array_append(array_append(wheel_ids, event_row.id), follow_row.id);
            wheel_count := wheel_count + 1;
        END LOOP;
    END LOOP;

    FOR event_row IN
        SELECT
            (item.value ->> 'id')::UUID AS id,
            coalesce((item.value ->> 'net_premium')::DOUBLE PRECISION,
                (item.value ->> 'premium')::DOUBLE PRECISION,
                0::DOUBLE PRECISION
            ) / 1000000::DOUBLE PRECISION AS premium,
            (item.value ->> 'is_itm')::BOOLEAN AS is_itm,
            (item.value ->> 'settled_at')::TIMESTAMPTZ AS settled_at
        FROM jsonb_array_elements(p_rows) WITH ORDINALITY AS item(value, ordinality)
        ORDER BY item.ordinality
    LOOP
        event_premium := event_row.premium;
        earns_bonus := event_row.id = ANY(wheel_ids);
        IF NOT earns_bonus AND event_row.is_itm IS FALSE AND event_row.settled_at IS NOT NULL THEN
            earns_bonus := (
                NOT week1_has_itm
                AND event_row.settled_at BETWEEN
                    TIMESTAMPTZ '2026-03-30 00:00:00+00'
                    AND TIMESTAMPTZ '2026-04-05 23:59:59+00'
            ) OR (
                NOT week2_has_itm
                AND event_row.settled_at BETWEEN
                    TIMESTAMPTZ '2026-04-06 00:00:00+00'
                    AND TIMESTAMPTZ '2026-04-12 23:59:59+00'
            );
        END IF;
        adjusted_premium := adjusted_premium
            + event_premium * CASE
                WHEN earns_bonus THEN 1.5::DOUBLE PRECISION
                ELSE 1::DOUBLE PRECISION
            END;
    END LOOP;

    FOR event_row IN
        SELECT (item.value ->> 'is_itm')::BOOLEAN AS is_itm
        FROM jsonb_array_elements(p_rows) AS item(value)
        WHERE item.value ->> 'settled_at' IS NOT NULL
        ORDER BY
            (item.value ->> 'settled_at')::TIMESTAMPTZ,
            (item.value ->> 'id')::UUID
    LOOP
        IF event_row.is_itm IS FALSE THEN
            current_streak := current_streak + 1;
            maximum_streak := greatest(maximum_streak, current_streak);
        ELSE
            current_streak := 0;
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'position_count', position_count,
        -- Keep the production FLOAT sum raw until each public field is built.
        'raw_collateral_usd', total_collateral,
        'adjusted_premium', private.b1n431_python_round(adjusted_premium, 6),
        'earning_rate', CASE
            WHEN total_collateral > 0 THEN private.b1n431_python_round(
                adjusted_premium / total_collateral,
                6
            )
            ELSE NULL
        END,
        'active_days', active_days,
        'wheel_count', wheel_count,
        'otm_streak', maximum_streak
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_legacy_leaderboard(
    p_start BIGINT,
    p_end BIGINT,
    p_limit INTEGER DEFAULT 50
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    snapshot_at TIMESTAMPTZ := statement_timestamp();
    result JSONB;
BEGIN
    IF p_start IS NULL OR p_end IS NULL OR p_start >= p_end THEN
        RAISE EXCEPTION 'start must be before end';
    END IF;
    IF p_end - p_start > 90 * 24 * 3600 THEN
        RAISE EXCEPTION 'range must not exceed 90 days';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'limit must be between 1 and 100';
    END IF;

    WITH source AS MATERIALIZED (
        SELECT
            e.id,
            lower(e.user_address) AS wallet,
            e.collateral_usd,
            e.net_premium,
            e.premium,
            e.is_put,
            e.asset,
            e.indexed_at,
            e.expiry,
            e.is_itm,
            e.settled_at
        FROM public.order_events AS e
        WHERE e.indexed_at >= to_timestamp(p_start)
          AND e.indexed_at <= to_timestamp(p_end)
          AND lower(e.user_address) <> ''
    ),
    grouped AS MATERIALIZED (
        SELECT
            source.wallet,
            private.b1n431_wallet_stats(
                jsonb_agg(
                    jsonb_build_object(
                        'id', source.id,
                        'collateral_usd', source.collateral_usd,
                        'net_premium', source.net_premium,
                        'premium', source.premium,
                        'is_put', source.is_put,
                        'asset', source.asset,
                        'indexed_at', source.indexed_at,
                        'expiry', source.expiry,
                        'is_itm', source.is_itm,
                        'settled_at', source.settled_at
                    ) ORDER BY source.indexed_at, source.id
                )
            ) AS payload
        FROM source
        GROUP BY source.wallet
    ),
    stats AS MATERIALIZED (
        SELECT
            grouped.wallet,
            (grouped.payload ->> 'position_count')::INTEGER AS position_count,
            (grouped.payload ->> 'raw_collateral_usd')::DOUBLE PRECISION
                AS raw_collateral_usd,
            (grouped.payload ->> 'adjusted_premium')::NUMERIC AS adjusted_premium,
            (grouped.payload ->> 'earning_rate')::NUMERIC AS earning_rate,
            (grouped.payload ->> 'active_days')::INTEGER AS active_days,
            (grouped.payload ->> 'wheel_count')::INTEGER AS wheel_count,
            (grouped.payload ->> 'otm_streak')::INTEGER AS otm_streak,
            (grouped.payload ->> 'raw_collateral_usd')::DOUBLE PRECISION >= 500
                AS qualified
        FROM grouped
    ),
    track1_rows AS (
        SELECT
            stats.*,
            CASE WHEN stats.qualified THEN
                row_number() OVER (
                    PARTITION BY stats.qualified
                    ORDER BY stats.earning_rate DESC NULLS LAST,
                        stats.raw_collateral_usd DESC,
                        stats.wallet ASC
                )
            END AS rank
        FROM stats
        ORDER BY stats.qualified DESC,
            stats.earning_rate DESC NULLS LAST,
            stats.raw_collateral_usd DESC,
            stats.wallet ASC
        LIMIT p_limit
    ),
    track2_rows AS (
        SELECT
            stats.*,
            CASE WHEN stats.qualified THEN
                row_number() OVER (
                    PARTITION BY stats.qualified
                    ORDER BY stats.otm_streak DESC,
                        stats.earning_rate DESC NULLS LAST,
                        stats.wallet ASC
                )
            END AS rank
        FROM stats
        ORDER BY stats.qualified DESC,
            stats.otm_streak DESC,
            stats.earning_rate DESC NULLS LAST,
            stats.wallet ASC
        LIMIT p_limit
    ),
    track1 AS (
        SELECT coalesce(jsonb_agg(
            jsonb_build_object(
                'rank', track1_rows.rank,
                'wallet', track1_rows.wallet,
                'earning_rate', track1_rows.earning_rate,
                'total_earned_usd', track1_rows.adjusted_premium,
                -- Internal transport field: the API applies predecessor Python
                -- float rounding and removes this key from the public wire.
                '_internal_raw_collateral_usd', track1_rows.raw_collateral_usd,
                'position_count', track1_rows.position_count,
                'wheel_count', track1_rows.wheel_count,
                'active_days', track1_rows.active_days,
                'qualified', track1_rows.qualified,
                'progress', jsonb_build_object(
                    'collateral_pct', private.b1n431_python_round(least(
                        track1_rows.raw_collateral_usd / 500::DOUBLE PRECISION,
                        1::DOUBLE PRECISION
                    ), 4)
                )
            ) ORDER BY track1_rows.qualified DESC,
                track1_rows.earning_rate DESC NULLS LAST,
                track1_rows.raw_collateral_usd DESC,
                track1_rows.wallet ASC
        ), '[]'::JSONB) AS payload
        FROM track1_rows
    ),
    track2 AS (
        SELECT coalesce(jsonb_agg(
            jsonb_build_object(
                'rank', track2_rows.rank,
                'wallet', track2_rows.wallet,
                'otm_streak', track2_rows.otm_streak,
                'position_count', track2_rows.position_count,
                'earning_rate', track2_rows.earning_rate,
                'qualified', track2_rows.qualified,
                'progress', jsonb_build_object(
                    'collateral_pct', private.b1n431_python_round(least(
                        track2_rows.raw_collateral_usd / 500::DOUBLE PRECISION,
                        1::DOUBLE PRECISION
                    ), 4)
                )
            ) ORDER BY track2_rows.qualified DESC,
                track2_rows.otm_streak DESC,
                track2_rows.earning_rate DESC NULLS LAST,
                track2_rows.wallet ASC
        ), '[]'::JSONB) AS payload
        FROM track2_rows
    ),
    metadata AS (
        SELECT
            count(*)::INTEGER AS total_participants,
            count(*) FILTER (WHERE stats.qualified)::INTEGER AS qualified_participants,
            coalesce(
                sum(stats.raw_collateral_usd), 0::DOUBLE PRECISION
            ) AS internal_raw_total_volume_usd
        FROM stats
    )
    SELECT jsonb_build_object(
        'track1', track1.payload,
        'track2', track2.payload,
        'meta', jsonb_build_object(
            'competition_start', p_start,
            'competition_end', p_end,
            'total_participants', metadata.total_participants,
            'qualified_participants', metadata.qualified_participants,
            -- Internal transport field; never returned by the backend API.
            '_internal_raw_total_volume_usd',
                metadata.internal_raw_total_volume_usd,
            'current_week', CASE
                WHEN snapshot_at < TIMESTAMPTZ '2026-04-06 00:00:00+00' THEN 1
                ELSE 2
            END,
            'limit', p_limit,
            'truncated', metadata.total_participants > p_limit,
            'as_of', snapshot_at,
            'cache_ttl_seconds', 60
        )
    )
    INTO result
    FROM track1 CROSS JOIN track2 CROSS JOIN metadata;

    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_legacy_leaderboard_me(
    p_address TEXT,
    p_start BIGINT,
    p_end BIGINT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    normalized_address TEXT := lower(p_address);
    snapshot_at TIMESTAMPTZ := statement_timestamp();
    stats JSONB;
BEGIN
    IF p_address IS NULL OR p_address !~ '^0x[0-9A-Fa-f]{40}$' THEN
        RAISE EXCEPTION 'invalid Ethereum address';
    END IF;
    IF p_start IS NULL OR p_end IS NULL OR p_start >= p_end THEN
        RAISE EXCEPTION 'start must be before end';
    END IF;
    IF p_end - p_start > 90 * 24 * 3600 THEN
        RAISE EXCEPTION 'range must not exceed 90 days';
    END IF;

    SELECT private.b1n431_wallet_stats(coalesce(jsonb_agg(
        jsonb_build_object(
            'id', e.id,
            'collateral_usd', e.collateral_usd,
            'net_premium', e.net_premium,
            'premium', e.premium,
            'is_put', e.is_put,
            'asset', e.asset,
            'indexed_at', e.indexed_at,
            'expiry', e.expiry,
            'is_itm', e.is_itm,
            'settled_at', e.settled_at
        ) ORDER BY e.indexed_at, e.id
    ), '[]'::JSONB))
    INTO stats
    FROM public.order_events AS e
    WHERE e.user_address = normalized_address
      AND e.indexed_at >= to_timestamp(p_start)
      AND e.indexed_at <= to_timestamp(p_end);

    RETURN jsonb_build_object(
        'wallet', normalized_address,
        'position_count', (stats ->> 'position_count')::INTEGER,
        -- Internal transport field; the API applies Python float round.
        '_internal_raw_collateral_usd',
            (stats ->> 'raw_collateral_usd')::DOUBLE PRECISION,
        'total_earned_usd', (stats ->> 'adjusted_premium')::NUMERIC,
        'earning_rate', (stats ->> 'earning_rate')::NUMERIC,
        'active_days', (stats ->> 'active_days')::INTEGER,
        'wheel_count', (stats ->> 'wheel_count')::INTEGER,
        'otm_streak', (stats ->> 'otm_streak')::INTEGER,
        'qualifies', (stats ->> 'raw_collateral_usd')::DOUBLE PRECISION >= 500,
        'as_of', snapshot_at,
        'cache_ttl_seconds', 60
    );
END;
$$;

REVOKE ALL ON SCHEMA private FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION private.b1n431_python_round(DOUBLE PRECISION, INTEGER)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION private.b1n431_wallet_stats(JSONB)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_legacy_leaderboard(BIGINT, BIGINT, INTEGER)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_legacy_leaderboard_me(TEXT, BIGINT, BIGINT)
    FROM PUBLIC, anon, authenticated;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT USAGE ON SCHEMA private TO service_role;
        GRANT EXECUTE ON FUNCTION private.b1n431_python_round(
            DOUBLE PRECISION, INTEGER
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION private.b1n431_wallet_stats(JSONB) TO service_role;
        GRANT EXECUTE ON FUNCTION public.b1nary_legacy_leaderboard(
            BIGINT, BIGINT, INTEGER
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION public.b1nary_legacy_leaderboard_me(
            TEXT, BIGINT, BIGINT
        ) TO service_role;
    END IF;
END;
$access$;
