-- B1N-423: aggregate MM exposure in Postgres so API egress is independent of
-- the number of historical order_events rows.

CREATE OR REPLACE FUNCTION public.v1_get_mm_exposure(
    p_mm_address TEXT,
    p_now_ts BIGINT
) RETURNS TABLE (
    active_quotes_count BIGINT,
    active_quotes_notional TEXT,
    open_positions_by_expiry JSONB,
    total_premium_earned TEXT,
    pending_settlement_count BIGINT
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
WITH quote_totals AS (
    SELECT
        COUNT(*)::BIGINT AS quote_count,
        COALESCE(SUM(q.max_amount), 0)::NUMERIC AS quote_notional
    FROM public.mm_quotes AS q
    WHERE q.mm_address = p_mm_address
      AND q.is_active = TRUE
      AND q.deadline > p_now_ts
),
fill_by_expiry AS MATERIALIZED (
    SELECT
        e.expiry AS expiry_value,
        COUNT(*)::BIGINT AS position_count,
        COALESCE(SUM(e.amount), 0)::NUMERIC AS total_amount,
        COALESCE(
            SUM(COALESCE(NULLIF(e.gross_premium, 0), e.premium, 0)), 0
        )::NUMERIC AS premium_total,
        COUNT(*) FILTER (
            WHERE e.expiry IS NOT NULL
              AND e.expiry <> 0
              AND e.expiry <= p_now_ts
              AND e.is_settled IS NOT TRUE
        )::BIGINT AS pending_count
    FROM public.order_events AS e
    WHERE e.mm_address = p_mm_address
    GROUP BY e.expiry
),
fill_totals AS (
    SELECT
        COALESCE(SUM(f.premium_total), 0)::NUMERIC AS premium_total,
        COALESCE(SUM(f.pending_count), 0)::BIGINT AS pending_count
    FROM fill_by_expiry AS f
),
open_by_expiry AS (
    SELECT
        f.expiry_value,
        f.position_count,
        f.total_amount
    FROM fill_by_expiry AS f
    WHERE f.expiry_value > p_now_ts
),
expiry_totals AS (
    SELECT COALESCE(
        JSONB_AGG(
            JSONB_BUILD_OBJECT(
                'expiry', o.expiry_value,
                'position_count', o.position_count,
                -- Keep NUMERIC exact across PostgREST's JSON boundary. Returning
                -- decimal values as JSON strings avoids a float round trip in
                -- Python clients before the API serializes its string fields.
                'total_amount', o.total_amount::TEXT
            )
            ORDER BY o.expiry_value
        ),
        '[]'::JSONB
    ) AS buckets
    FROM open_by_expiry AS o
)
SELECT
    q.quote_count,
    q.quote_notional::TEXT,
    x.buckets,
    f.premium_total::TEXT,
    f.pending_count
FROM quote_totals AS q
CROSS JOIN fill_totals AS f
CROSS JOIN expiry_totals AS x;
$$;

COMMENT ON FUNCTION public.v1_get_mm_exposure(TEXT, BIGINT) IS
    'Returns one bounded aggregate row for the authenticated MM exposure API.';

REVOKE EXECUTE ON FUNCTION public.v1_get_mm_exposure(TEXT, BIGINT) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION public.v1_get_mm_exposure(TEXT, BIGINT)
            TO service_role;
    END IF;
END;
$access$;

NOTIFY pgrst, 'reload schema';
