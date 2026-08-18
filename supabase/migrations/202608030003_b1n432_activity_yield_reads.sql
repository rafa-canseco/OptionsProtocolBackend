-- B1N-432: bounded activity and yield reads.
-- Additive only: no table, row, or historical migration is removed.

CREATE INDEX IF NOT EXISTS idx_b1n432_yield_allocations_history
    ON public.yield_allocations (user_address, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_b1n432_yield_positions_user_page
    ON public.yield_positions (user_address, deposited_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_b1n432_yield_positions_overlap_active
    ON public.yield_positions (deposited_at, asset)
    INCLUDE (collateral_amount, user_address)
    WHERE settled_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_b1n432_yield_positions_overlap_settled
    ON public.yield_positions (settled_at, deposited_at, asset)
    INCLUDE (collateral_amount, user_address)
    WHERE settled_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_b1n432_yield_distributions_period_end
    ON public.yield_distributions (period_end DESC);

CREATE OR REPLACE FUNCTION public.b1nary_activity_summary(
    p_addresses TEXT[]
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    result JSONB;
BEGIN
    IF p_addresses IS NULL
        OR cardinality(p_addresses) < 1
        OR cardinality(p_addresses) > 2
        OR EXISTS (
            SELECT 1
            FROM unnest(p_addresses) AS address(value)
            WHERE address.value IS NULL
               OR address.value <> lower(address.value)
               OR address.value !~ '^0x[0-9a-f]{40}$'
        ) THEN
        RAISE EXCEPTION 'one or two normalized addresses are required';
    END IF;

    -- Use float8 for the legacy Python-float arithmetic, but leave final
    -- Python round(value, places) to the route so ties keep Python semantics.
    WITH matching AS (
        SELECT DISTINCT ON (event.id)
            event.id,
            event.collateral,
            event.collateral_usd,
            event.net_premium,
            event.premium,
            event.is_put,
            event.strike_price,
            event.asset,
            event.indexed_at
        FROM public.order_events AS event
        WHERE event.user_address = ANY (p_addresses)
        ORDER BY event.id
    ), totals AS (
        SELECT
            count(*)::BIGINT AS position_count,
            coalesce(sum(
                CASE
                    WHEN event.is_put IS NOT FALSE
                        THEN trunc(coalesce(event.collateral, 0))::DOUBLE PRECISION
                            / 1000000::DOUBLE PRECISION
                    ELSE
                        trunc(coalesce(event.collateral, 0))::DOUBLE PRECISION
                        / CASE WHEN lower(coalesce(event.asset, 'eth')) = 'btc'
                            THEN 100000000::DOUBLE PRECISION
                            ELSE 1000000000000000000::DOUBLE PRECISION
                          END
                        * trunc(coalesce(event.strike_price, 0))::DOUBLE PRECISION
                        / 100000000::DOUBLE PRECISION
                END
            ), 0::DOUBLE PRECISION) AS total_volume,
            coalesce(sum(
                trunc(coalesce(nullif(event.net_premium, 0), event.premium, 0))
                    ::DOUBLE PRECISION / 1000000::DOUBLE PRECISION
            ), 0::DOUBLE PRECISION) AS total_premium,
            count(DISTINCT (
                event.indexed_at AT TIME ZONE 'UTC'
            )::DATE)::BIGINT AS active_days,
            min((event.indexed_at AT TIME ZONE 'UTC')::DATE) AS first_date,
            coalesce(sum(
                coalesce(event.collateral_usd, 0)::DOUBLE PRECISION
            ), 0::DOUBLE PRECISION) AS stored_collateral
        FROM matching AS event
    )
    SELECT jsonb_build_object(
        'total_volume', totals.total_volume::TEXT,
        'total_premium', totals.total_premium::TEXT,
        'position_count', totals.position_count,
        'active_days', totals.active_days,
        'days_since_first', CASE
            WHEN totals.first_date IS NULL THEN 0
            ELSE ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::DATE - totals.first_date)
        END,
        'total_collateral_usd', totals.stored_collateral::TEXT
    )
    INTO result
    FROM totals;

    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_yield_user_summary(
    p_user_address TEXT,
    p_as_of TIMESTAMPTZ,
    p_accrued JSONB,
    p_protocol_fee_bps INTEGER
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    normalized_address TEXT := lower(p_user_address);
    period_start TIMESTAMPTZ;
    result_rows JSONB;
BEGIN
    IF p_user_address IS NULL
        OR p_user_address <> normalized_address
        OR p_user_address !~ '^0x[0-9a-f]{40}$'
        OR p_as_of IS NULL THEN
        RAISE EXCEPTION 'normalized user address and as_of are required';
    END IF;
    IF p_accrued IS NULL OR jsonb_typeof(p_accrued) <> 'object'
        OR p_protocol_fee_bps IS NULL
        OR p_protocol_fee_bps < 0
        OR p_protocol_fee_bps > 10000 THEN
        RAISE EXCEPTION 'accrued values and protocol fee are required';
    END IF;

    SELECT coalesce(max(distribution.period_end), '2026-04-02T00:00:00Z'::TIMESTAMPTZ)
    INTO period_start
    FROM public.yield_distributions AS distribution;

    WITH known_assets(asset, accrued_raw) AS (
        VALUES
            ('usdc'::TEXT, CASE WHEN jsonb_typeof(p_accrued -> 'usdc') = 'number'
                THEN (p_accrued ->> 'usdc')::NUMERIC ELSE 0::NUMERIC END),
            ('eth'::TEXT, CASE WHEN jsonb_typeof(p_accrued -> 'eth') = 'number'
                THEN (p_accrued ->> 'eth')::NUMERIC ELSE 0::NUMERIC END),
            ('btc'::TEXT, CASE WHEN jsonb_typeof(p_accrued -> 'btc') = 'number'
                THEN (p_accrued ->> 'btc')::NUMERIC ELSE 0::NUMERIC END)
    ), asset_amounts AS (
        SELECT
            known_assets.asset,
            trunc(
                greatest(known_assets.accrued_raw, 0::NUMERIC)
                * (10000 - p_protocol_fee_bps)::NUMERIC / 10000::NUMERIC
            ) AS distributable_raw
        FROM known_assets
    ), allocation_totals AS (
        SELECT
            allocation.asset,
            coalesce(sum(allocation.amount) FILTER (
                WHERE allocation.status = 'delivered'
            ), 0)::NUMERIC AS delivered,
            coalesce(sum(allocation.amount) FILTER (
                WHERE allocation.status <> 'delivered'
            ), 0)::NUMERIC AS pending
        FROM public.yield_allocations AS allocation
        JOIN known_assets ON known_assets.asset = allocation.asset
        WHERE allocation.user_address = normalized_address
        GROUP BY allocation.asset
    ), overlap_positions AS NOT MATERIALIZED (
        SELECT
            position.asset,
            position.user_address,
            position.collateral_amount::NUMERIC
                * extract(EPOCH FROM (
                    p_as_of - greatest(position.deposited_at, period_start)
                ))::NUMERIC AS weight
        FROM public.yield_positions AS position
        JOIN known_assets ON known_assets.asset = position.asset
        WHERE position.settled_at IS NULL
          AND position.deposited_at < p_as_of
          AND p_as_of > greatest(position.deposited_at, period_start)
        UNION ALL
        SELECT
            position.asset,
            position.user_address,
            position.collateral_amount::NUMERIC
                * extract(EPOCH FROM (
                    least(position.settled_at, p_as_of)
                    - greatest(position.deposited_at, period_start)
                ))::NUMERIC AS weight
        FROM public.yield_positions AS position
        JOIN known_assets ON known_assets.asset = position.asset
        WHERE position.settled_at IS NOT NULL
          AND position.settled_at > period_start
          AND position.deposited_at < p_as_of
          AND least(position.settled_at, p_as_of)
              > greatest(position.deposited_at, period_start)
    ), global_weights AS (
        SELECT overlap.asset, sum(overlap.weight) AS global_weight
        FROM overlap_positions AS overlap
        GROUP BY overlap.asset
    ), user_estimates AS (
        SELECT
            overlap.asset,
            sum(overlap.weight) AS user_weight,
            sum(trunc(
                asset_amounts.distributable_raw
                * overlap.weight / global_weights.global_weight
            )) AS estimated_raw
        FROM overlap_positions AS overlap
        JOIN global_weights USING (asset)
        JOIN asset_amounts USING (asset)
        WHERE overlap.user_address = normalized_address
          AND global_weights.global_weight > 0
        GROUP BY overlap.asset
    )
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'asset', known_assets.asset,
            'pending_raw', coalesce(allocation_totals.pending, 0)::TEXT,
            'delivered_raw', coalesce(allocation_totals.delivered, 0)::TEXT,
            'user_weight', coalesce(user_estimates.user_weight, 0)::TEXT,
            'global_weight', coalesce(global_weights.global_weight, 0)::TEXT,
            'estimated_accruing_raw', coalesce(user_estimates.estimated_raw, 0)::TEXT
        ) ORDER BY known_assets.asset
    ) FILTER (
        WHERE coalesce(allocation_totals.pending, 0) <> 0
           OR coalesce(allocation_totals.delivered, 0) <> 0
           OR coalesce(user_estimates.user_weight, 0) > 0
    ), '[]'::JSONB)
    INTO result_rows
    FROM known_assets
    LEFT JOIN allocation_totals USING (asset)
    LEFT JOIN global_weights USING (asset)
    LEFT JOIN user_estimates USING (asset);

    RETURN jsonb_build_object(
        'rows', result_rows,
        'period_start', period_start,
        'as_of', p_as_of
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_yield_position_page(
    p_user_address TEXT,
    p_limit INTEGER,
    p_cursor_at TIMESTAMPTZ,
    p_cursor_id UUID,
    p_watermark TIMESTAMPTZ,
    p_wallet_fingerprint TEXT,
    p_as_of TIMESTAMPTZ,
    p_period_start TIMESTAMPTZ,
    p_accrued JSONB,
    p_protocol_fee_bps INTEGER
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    normalized_address TEXT := lower(p_user_address);
    effective_watermark TIMESTAMPTZ := coalesce(p_watermark, statement_timestamp());
    resolved_fingerprint TEXT;
    period_start TIMESTAMPTZ;
    result_rows JSONB;
    result_totals JSONB;
BEGIN
    IF p_user_address IS NULL
        OR p_user_address <> normalized_address
        OR p_user_address !~ '^0x[0-9a-f]{40}$'
        OR p_as_of IS NULL THEN
        RAISE EXCEPTION 'normalized user address and as_of are required';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'yield position limit must be between 1 and 100';
    END IF;
    IF p_accrued IS NULL OR jsonb_typeof(p_accrued) <> 'object'
        OR p_protocol_fee_bps IS NULL
        OR p_protocol_fee_bps < 0
        OR p_protocol_fee_bps > 10000 THEN
        RAISE EXCEPTION 'accrued values and protocol fee are required';
    END IF;
    IF (p_cursor_at IS NULL) <> (p_cursor_id IS NULL) THEN
        RAISE EXCEPTION 'both cursor components are required';
    END IF;
    IF p_cursor_at IS NULL AND p_period_start IS NOT NULL THEN
        RAISE EXCEPTION 'first yield position page must resolve period_start';
    END IF;
    IF p_cursor_at IS NOT NULL AND p_period_start IS NULL THEN
        RAISE EXCEPTION 'yield position continuation requires period_start';
    END IF;

    SELECT encode(
        sha256(convert_to(
            'base:' || length(normalized_address)::TEXT || ':' || normalized_address,
            'UTF8'
        )),
        'hex'
    ) INTO resolved_fingerprint;
    IF p_wallet_fingerprint IS NOT NULL
        AND p_wallet_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid wallet fingerprint';
    END IF;
    IF p_wallet_fingerprint IS NOT NULL
        AND p_wallet_fingerprint <> resolved_fingerprint THEN
        RETURN jsonb_build_object(
            'filter_mismatch', TRUE,
            'wallet_fingerprint', resolved_fingerprint,
            'watermark', effective_watermark,
            'rows', '[]'::JSONB,
            'totals', '[]'::JSONB
        );
    END IF;

    IF p_period_start IS NULL THEN
        SELECT coalesce(
            max(distribution.period_end),
            '2026-04-02T00:00:00Z'::TIMESTAMPTZ
        )
        INTO period_start
        FROM public.yield_distributions AS distribution;
    ELSE
        period_start := p_period_start;
    END IF;

    WITH known_assets(asset, accrued_raw) AS (
        VALUES
            ('usdc'::TEXT, CASE WHEN jsonb_typeof(p_accrued -> 'usdc') = 'number'
                THEN (p_accrued ->> 'usdc')::NUMERIC ELSE 0::NUMERIC END),
            ('eth'::TEXT, CASE WHEN jsonb_typeof(p_accrued -> 'eth') = 'number'
                THEN (p_accrued ->> 'eth')::NUMERIC ELSE 0::NUMERIC END),
            ('btc'::TEXT, CASE WHEN jsonb_typeof(p_accrued -> 'btc') = 'number'
                THEN (p_accrued ->> 'btc')::NUMERIC ELSE 0::NUMERIC END)
    ), asset_amounts AS (
        SELECT
            known_assets.asset,
            trunc(
                greatest(known_assets.accrued_raw, 0::NUMERIC)
                * (10000 - p_protocol_fee_bps)::NUMERIC / 10000::NUMERIC
            ) AS distributable_raw
        FROM known_assets
    ), overlap_positions AS NOT MATERIALIZED (
        SELECT
            position.asset,
            position.user_address,
            position.collateral_amount::NUMERIC
                * extract(EPOCH FROM (
                    p_as_of - greatest(position.deposited_at, period_start)
                ))::NUMERIC AS weight
        FROM public.yield_positions AS position
        JOIN known_assets ON known_assets.asset = position.asset
        WHERE position.settled_at IS NULL
          AND position.deposited_at < p_as_of
          AND p_as_of > greatest(position.deposited_at, period_start)
        UNION ALL
        SELECT
            position.asset,
            position.user_address,
            position.collateral_amount::NUMERIC
                * extract(EPOCH FROM (
                    least(position.settled_at, p_as_of)
                    - greatest(position.deposited_at, period_start)
                ))::NUMERIC AS weight
        FROM public.yield_positions AS position
        JOIN known_assets ON known_assets.asset = position.asset
        WHERE position.settled_at IS NOT NULL
          AND position.settled_at > period_start
          AND position.deposited_at < p_as_of
          AND least(position.settled_at, p_as_of)
              > greatest(position.deposited_at, period_start)
    ), global_weights AS (
        SELECT overlap.asset, sum(overlap.weight) AS global_weight
        FROM overlap_positions AS overlap
        GROUP BY overlap.asset
    ), user_assets AS (
        SELECT DISTINCT position.asset
        FROM public.yield_positions AS position
        JOIN known_assets ON known_assets.asset = position.asset
        WHERE position.user_address = normalized_address
    ), user_estimates AS (
        SELECT
            overlap.asset,
            sum(trunc(
                asset_amounts.distributable_raw
                * overlap.weight / global_weights.global_weight
            )) AS estimated_raw
        FROM overlap_positions AS overlap
        JOIN global_weights USING (asset)
        JOIN asset_amounts USING (asset)
        WHERE overlap.user_address = normalized_address
          AND global_weights.global_weight > 0
        GROUP BY overlap.asset
    )
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'asset', user_assets.asset,
            'estimated_yield_raw', coalesce(user_estimates.estimated_raw, 0)::TEXT
        ) ORDER BY user_assets.asset
    ), '[]'::JSONB)
    INTO result_totals
    FROM user_assets
    LEFT JOIN user_estimates USING (asset);

    WITH overlap_positions AS NOT MATERIALIZED (
        SELECT
            position.asset,
            position.collateral_amount::NUMERIC
                * extract(EPOCH FROM (
                    p_as_of - greatest(position.deposited_at, period_start)
                ))::NUMERIC AS weight
        FROM public.yield_positions AS position
        WHERE position.asset IN ('usdc', 'eth', 'btc')
          AND position.settled_at IS NULL
          AND position.deposited_at < p_as_of
          AND p_as_of > greatest(position.deposited_at, period_start)
        UNION ALL
        SELECT
            position.asset,
            position.collateral_amount::NUMERIC
                * extract(EPOCH FROM (
                    least(position.settled_at, p_as_of)
                    - greatest(position.deposited_at, period_start)
                ))::NUMERIC AS weight
        FROM public.yield_positions AS position
        WHERE position.asset IN ('usdc', 'eth', 'btc')
          AND position.settled_at IS NOT NULL
          AND position.settled_at > period_start
          AND position.deposited_at < p_as_of
          AND least(position.settled_at, p_as_of)
              > greatest(position.deposited_at, period_start)
    ), global_weights AS (
        SELECT overlap.asset, sum(overlap.weight) AS global_weight
        FROM overlap_positions AS overlap
        GROUP BY overlap.asset
    ), page AS (
        SELECT
            position.id,
            position.vault_id,
            position.asset,
            position.collateral_amount,
            position.deposited_at,
            position.settled_at
        FROM public.yield_positions AS position
        WHERE position.user_address = normalized_address
          AND position.asset IN ('usdc', 'eth', 'btc')
          AND position.created_at <= effective_watermark
          AND (
              p_cursor_at IS NULL
              OR (position.deposited_at, position.id) < (p_cursor_at, p_cursor_id)
          )
        ORDER BY position.deposited_at DESC, position.id DESC
        LIMIT p_limit + 1
    ), projected AS (
        SELECT
            page.*,
            CASE
                WHEN page.deposited_at < p_as_of
                 AND (page.settled_at IS NULL OR page.settled_at > period_start)
                 AND least(coalesce(page.settled_at, p_as_of), p_as_of)
                     > greatest(page.deposited_at, period_start)
                THEN page.collateral_amount::NUMERIC
                    * extract(EPOCH FROM (
                        least(coalesce(page.settled_at, p_as_of), p_as_of)
                        - greatest(page.deposited_at, period_start)
                    ))::NUMERIC
                ELSE 0::NUMERIC
            END AS position_weight,
            coalesce(global_weights.global_weight, 0::NUMERIC) AS global_weight
        FROM page
        LEFT JOIN global_weights USING (asset)
    )
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id', projected.id,
            'vault_id', projected.vault_id,
            'asset', projected.asset,
            'collateral_amount', projected.collateral_amount::TEXT,
            'deposited_at', projected.deposited_at,
            'settled_at', projected.settled_at,
            'position_weight', projected.position_weight::TEXT,
            'global_weight', projected.global_weight::TEXT
        ) ORDER BY projected.deposited_at DESC, projected.id DESC
    ), '[]'::JSONB)
    INTO result_rows
    FROM projected;

    RETURN jsonb_build_object(
        'rows', result_rows,
        'totals', result_totals,
        'period_start', period_start,
        'as_of', p_as_of,
        'watermark', effective_watermark,
        'wallet_fingerprint', resolved_fingerprint
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_yield_history_page(
    p_user_address TEXT,
    p_limit INTEGER,
    p_cursor_at TIMESTAMPTZ,
    p_cursor_id UUID,
    p_watermark TIMESTAMPTZ,
    p_wallet_fingerprint TEXT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    normalized_address TEXT := lower(p_user_address);
    effective_watermark TIMESTAMPTZ := coalesce(p_watermark, statement_timestamp());
    resolved_fingerprint TEXT;
    result_rows JSONB;
BEGIN
    IF p_user_address IS NULL
        OR p_user_address <> normalized_address
        OR p_user_address !~ '^0x[0-9a-f]{40}$' THEN
        RAISE EXCEPTION 'normalized user address is required';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'yield history limit must be between 1 and 100';
    END IF;
    IF (p_cursor_at IS NULL) <> (p_cursor_id IS NULL) THEN
        RAISE EXCEPTION 'both cursor components are required';
    END IF;

    SELECT encode(
        sha256(convert_to(
            'base:' || length(normalized_address)::TEXT || ':' || normalized_address,
            'UTF8'
        )),
        'hex'
    ) INTO resolved_fingerprint;
    IF p_wallet_fingerprint IS NOT NULL
        AND p_wallet_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid wallet fingerprint';
    END IF;
    IF p_wallet_fingerprint IS NOT NULL
        AND p_wallet_fingerprint <> resolved_fingerprint THEN
        RETURN jsonb_build_object(
            'filter_mismatch', TRUE,
            'wallet_fingerprint', resolved_fingerprint,
            'watermark', effective_watermark,
            'rows', '[]'::JSONB
        );
    END IF;

    WITH page AS (
        SELECT
            allocation.id,
            allocation.distribution_id,
            allocation.asset,
            allocation.amount,
            allocation.status,
            allocation.airdrop_tx_hash,
            allocation.created_at
        FROM public.yield_allocations AS allocation
        WHERE allocation.user_address = normalized_address
          AND allocation.created_at <= effective_watermark
          AND (
              p_cursor_at IS NULL
              OR (allocation.created_at, allocation.id) < (p_cursor_at, p_cursor_id)
          )
        ORDER BY allocation.created_at DESC, allocation.id DESC
        LIMIT p_limit + 1
    )
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id', page.id,
            'distribution_id', page.distribution_id,
            'asset', page.asset,
            'amount', page.amount::TEXT,
            'status', page.status,
            'airdrop_tx_hash', page.airdrop_tx_hash,
            'created_at', page.created_at
        ) ORDER BY page.created_at DESC, page.id DESC
    ), '[]'::JSONB)
    INTO result_rows
    FROM page;

    RETURN jsonb_build_object(
        'rows', result_rows,
        'watermark', effective_watermark,
        'wallet_fingerprint', resolved_fingerprint
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_yield_stats()
RETURNS JSONB
LANGUAGE sql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    WITH known_assets(asset) AS (
        VALUES ('usdc'::TEXT), ('eth'::TEXT), ('btc'::TEXT)
    ), totals AS (
        SELECT
            distribution.asset,
            sum(distribution.total_yield)::NUMERIC AS total_yield,
            sum(distribution.platform_fee)::NUMERIC AS total_fees,
            count(*)::BIGINT AS distributions
        FROM public.yield_distributions AS distribution
        JOIN known_assets ON known_assets.asset = distribution.asset
        GROUP BY distribution.asset
    )
    SELECT jsonb_build_object(
        'rows', jsonb_agg(
            jsonb_build_object(
                'asset', known_assets.asset,
                'total_yield_raw', coalesce(totals.total_yield, 0)::TEXT,
                'total_fees_raw', coalesce(totals.total_fees, 0)::TEXT,
                'distributions', coalesce(totals.distributions, 0)
            ) ORDER BY known_assets.asset
        ),
        'as_of', statement_timestamp()
    )
    FROM known_assets
    LEFT JOIN totals USING (asset);
$$;

REVOKE ALL ON FUNCTION public.b1nary_activity_summary(TEXT[])
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_yield_user_summary(
    TEXT, TIMESTAMPTZ, JSONB, INTEGER
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_yield_position_page(
    TEXT, INTEGER, TIMESTAMPTZ, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ,
    TIMESTAMPTZ, JSONB, INTEGER
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_yield_history_page(
    TEXT, INTEGER, TIMESTAMPTZ, UUID, TIMESTAMPTZ, TEXT
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_yield_stats()
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.b1nary_activity_summary(TEXT[])
    TO service_role;
GRANT EXECUTE ON FUNCTION public.b1nary_yield_user_summary(
    TEXT, TIMESTAMPTZ, JSONB, INTEGER
) TO service_role;
GRANT EXECUTE ON FUNCTION public.b1nary_yield_position_page(
    TEXT, INTEGER, TIMESTAMPTZ, UUID, TIMESTAMPTZ, TEXT, TIMESTAMPTZ,
    TIMESTAMPTZ, JSONB, INTEGER
) TO service_role;
GRANT EXECUTE ON FUNCTION public.b1nary_yield_history_page(
    TEXT, INTEGER, TIMESTAMPTZ, UUID, TIMESTAMPTZ, TEXT
) TO service_role;
GRANT EXECUTE ON FUNCTION public.b1nary_yield_stats()
    TO service_role;

NOTIFY pgrst, 'reload schema';
