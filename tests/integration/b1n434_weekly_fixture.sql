-- Exact 100,000-row weekly source, including one 80k-row wallet and 2,000
-- additional wallets so both source and result row caps are exceeded.
INSERT INTO public.order_events (
    id,
    tx_hash,
    block_number,
    user_address,
    amount,
    premium,
    gross_premium,
    net_premium,
    strike_price,
    collateral_usd,
    is_put,
    asset,
    indexed_at,
    expiry,
    is_settled,
    is_itm,
    settled_at
)
SELECT
    (
        substr(md5('b1n434-event-' || sequence_number::TEXT), 1, 8) || '-' ||
        substr(md5('b1n434-event-' || sequence_number::TEXT), 9, 4) || '-' ||
        substr(md5('b1n434-event-' || sequence_number::TEXT), 13, 4) || '-' ||
        substr(md5('b1n434-event-' || sequence_number::TEXT), 17, 4) || '-' ||
        substr(md5('b1n434-event-' || sequence_number::TEXT), 21, 12)
    )::UUID,
    '0x' || lpad(to_hex(sequence_number), 64, '0'),
    sequence_number,
    CASE
        WHEN sequence_number <= 80000 THEN
            '0x0000000000000000000000000000000000000002'
        ELSE
            '0x' || lpad((5000 + sequence_number % 2000)::TEXT, 40, '0')
    END,
    1000000,
    CASE WHEN sequence_number % 10 = 0 THEN 200000 ELSE 100000 END,
    CASE WHEN sequence_number % 10 = 0 THEN 200000 ELSE 100000 END,
    CASE WHEN sequence_number % 10 = 0 THEN 0 ELSE 100000 END,
    2100 * 100000000::NUMERIC,
    1,
    TRUE,
    'eth',
    TIMESTAMPTZ '2026-04-03 08:00:00+00'
        + (sequence_number - 1) * INTERVAL '1 second',
    extract(epoch FROM TIMESTAMPTZ '2026-04-10 08:00:00+00')::BIGINT,
    sequence_number % 20 = 0,
    CASE WHEN sequence_number % 20 = 0 THEN TRUE ELSE NULL END,
    CASE WHEN sequence_number % 20 = 0 THEN
        TIMESTAMPTZ '2026-04-04 08:00:00+00'
            + sequence_number * INTERVAL '1 second'
        ELSE NULL
    END
FROM generate_series(1, 100000) AS sequence_number;

-- Compact named semantics inside the scale wallet: normalization, null and
-- zero-net fallback, an assigned call, non-positive loss, missing strike,
-- OTM settlement, and a source row with no wallet that still counts in the
-- platform position total.
UPDATE public.order_events SET user_address = upper(user_address)
WHERE block_number = 1;
UPDATE public.order_events SET net_premium = NULL, premium = 300000
WHERE block_number = 2;
UPDATE public.order_events SET net_premium = 0, premium = 250000
WHERE block_number = 3;
UPDATE public.order_events SET is_settled = TRUE, is_itm = FALSE
WHERE block_number = 4;
UPDATE public.order_events SET
    is_settled = TRUE,
    is_itm = TRUE,
    is_put = FALSE,
    strike_price = 1900 * 100000000::NUMERIC,
    amount = 10000000
WHERE block_number = 5;
UPDATE public.order_events SET
    is_settled = TRUE,
    is_itm = TRUE,
    is_put = TRUE,
    strike_price = 1900 * 100000000::NUMERIC,
    amount = 100000000
WHERE block_number = 6;
UPDATE public.order_events SET
    is_settled = TRUE,
    is_itm = TRUE,
    is_put = TRUE,
    strike_price = NULL
WHERE block_number = 7;
UPDATE public.order_events SET is_settled = TRUE, is_itm = FALSE
WHERE block_number = 8;
UPDATE public.order_events SET user_address = ''
WHERE block_number = 9;
UPDATE public.order_events SET
    is_settled = TRUE,
    is_itm = TRUE,
    is_put = NULL,
    strike_price = 1900 * 100000000::NUMERIC,
    amount = 10000000
WHERE block_number = 10;

-- Binary64 transition fixture on wallet 5001. In source order, Python adds
-- 361746/1e6 then 735104/1e6 and rounds the binary64 sum to 1.0968. Remaining
-- premiums are zero; row 84001 contributes an assignment loss. NUMERIC source
-- columns exclude malformed stored strings by schema, while NULL net premium,
-- zero-net fallback, zero values, and NULL strike remain covered above/below.
UPDATE public.order_events SET
    premium = 0,
    net_premium = 0,
    is_settled = FALSE,
    is_itm = NULL
WHERE block_number IN (80001, 82001, 84001, 86001, 88001,
                       90001, 92001, 94001, 96001, 98001);
UPDATE public.order_events SET premium = 361746, net_premium = 361746
WHERE block_number = 80001;
UPDATE public.order_events SET premium = 735104, net_premium = 735104
WHERE block_number = 82001;
UPDATE public.order_events SET
    is_settled = TRUE,
    is_itm = TRUE,
    is_put = TRUE,
    strike_price = 2100 * 100000000::NUMERIC,
    amount = 1000000
WHERE block_number = 84001;

-- Ordered report-total discriminator. Wallet 5002 contributes 1e11 before
-- wallets 5003..5010 each contribute 0.0001. With the preceding wallet totals
-- and remaining fixture wallets, pinned Python binary64 order rounds to
-- 100000010990.3477; exact NUMERIC and reverse-order addition both produce
-- 100000010990.3476. This makes an exact or reordered implementation fail.
UPDATE public.order_events SET premium = 0, net_premium = 0
WHERE user_address IN (
    '0x0000000000000000000000000000000000005002',
    '0x0000000000000000000000000000000000005003',
    '0x0000000000000000000000000000000000005004',
    '0x0000000000000000000000000000000000005005',
    '0x0000000000000000000000000000000000005006',
    '0x0000000000000000000000000000000000005007',
    '0x0000000000000000000000000000000000005008',
    '0x0000000000000000000000000000000000005009',
    '0x0000000000000000000000000000000000005010'
);
UPDATE public.order_events
SET premium = 100000000000000000, net_premium = 100000000000000000
WHERE block_number = 80002;
UPDATE public.order_events SET premium = 100, net_premium = 100
WHERE block_number BETWEEN 80003 AND 80010;

-- Strictly earlier cumulative inputs. The target-week retry must never read the
-- row it replaces; wallet 5001 also exercises previous + unrounded binary64 P&L.
INSERT INTO public.user_weekly_results (
    user_address,
    week_start,
    week_end,
    positions_opened,
    total_simulated_premium,
    assignments,
    simulated_pnl,
    cumulative_pnl
) VALUES
(
    '0x0000000000000000000000000000000000000002',
    '2026-03-27', '2026-04-03', 1, 12.3456, 0, 12.3456, 12.3456
),
(
    '0x0000000000000000000000000000000000005001',
    '2026-03-27', '2026-04-03', 1, 0.0001, 0, 0.0001, 0.00005
);

ANALYZE public.order_events;

-- Fixture-only controls and fixed-cardinality evidence helpers.
CREATE TABLE public.b1n434_fixture_control (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
    fail_report BOOLEAN NOT NULL DEFAULT FALSE,
    hold_report_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
);
INSERT INTO public.b1n434_fixture_control DEFAULT VALUES;

CREATE OR REPLACE FUNCTION public.b1n434_fixture_report_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
DECLARE
    hold_seconds DOUBLE PRECISION;
BEGIN
    IF (SELECT fail_report FROM public.b1n434_fixture_control WHERE singleton) THEN
        RAISE EXCEPTION 'b1n434 induced report failure';
    END IF;
    SELECT hold_report_seconds
    INTO hold_seconds
    FROM public.b1n434_fixture_control
    WHERE singleton
    FOR UPDATE;
    IF hold_seconds > 0 THEN
        UPDATE public.b1n434_fixture_control
        SET hold_report_seconds = 0
        WHERE singleton;
        PERFORM pg_sleep(hold_seconds);
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER b1n434_fixture_report_guard
BEFORE INSERT OR UPDATE ON public.weekly_reports
FOR EACH ROW EXECUTE FUNCTION public.b1n434_fixture_report_guard();

CREATE OR REPLACE FUNCTION public.b1n434_fixture_set_failure(p_enabled BOOLEAN)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
BEGIN
    UPDATE public.b1n434_fixture_control SET fail_report = p_enabled WHERE singleton;
    RETURN jsonb_build_object('enabled', p_enabled);
END;
$function$;

CREATE OR REPLACE FUNCTION public.b1n434_fixture_set_hold(p_seconds DOUBLE PRECISION)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
BEGIN
    IF p_seconds < 0 OR p_seconds > 10 THEN
        RAISE EXCEPTION 'hold seconds out of range';
    END IF;
    UPDATE public.b1n434_fixture_control
    SET hold_report_seconds = p_seconds
    WHERE singleton;
    RETURN jsonb_build_object('seconds', p_seconds);
END;
$function$;

CREATE OR REPLACE FUNCTION public.b1n434_fixture_state()
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
    SELECT jsonb_build_object(
        'user_rows', (SELECT count(*) FROM public.user_weekly_results),
        'report_rows', (SELECT count(*) FROM public.weekly_reports),
        -- Digest every physical column, including identities and both timestamps,
        -- in a UTC canonical representation so retry evidence is byte-stable.
        'digest', encode(digest(
            coalesce((
                SELECT string_agg(concat_ws('|',
                    result.id::TEXT, result.user_address, result.week_start,
                    result.week_end, result.positions_opened::TEXT,
                    result.total_simulated_premium::TEXT,
                    result.assignments::TEXT, result.simulated_pnl::TEXT,
                    result.cumulative_pnl::TEXT,
                    to_char(result.created_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US'),
                    to_char(result.updated_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US')
                ), E'\n' ORDER BY result.user_address, result.week_start)
                FROM public.user_weekly_results AS result
            ), '') || E'\n#\n' || coalesce((
                SELECT string_agg(concat_ws('|',
                    report.id::TEXT, report.week_start, report.week_end,
                    report.total_users::TEXT, report.total_positions::TEXT,
                    report.total_simulated_premium::TEXT,
                    report.total_assignments::TEXT, report.eth_open::TEXT,
                    report.eth_close::TEXT, report.eth_high::TEXT,
                    report.eth_low::TEXT, report.narrative_data::TEXT,
                    to_char(report.created_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US'),
                    to_char(report.updated_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US')
                ), E'\n' ORDER BY report.week_start)
                FROM public.weekly_reports AS report
            ), ''),
            'sha256'
        ), 'hex')
    );
$function$;

CREATE OR REPLACE FUNCTION public.b1n434_fixture_try_week_lock(
    p_week_start TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
DECLARE
    canonical_week_start TEXT := to_char(
        p_week_start AT TIME ZONE 'UTC',
        'YYYY-MM-DD'
    );
    canonical_lock_input TEXT;
    canonical_lock_key BIGINT;
BEGIN
    canonical_lock_input := 'b1n434:' || canonical_week_start;
    canonical_lock_key := hashtextextended(canonical_lock_input, 434);
    RETURN jsonb_build_object(
        'canonical_week_start', canonical_week_start,
        'canonical_lock_input', canonical_lock_input,
        'hash_seed', 434,
        'canonical_lock_key', canonical_lock_key,
        'acquired', pg_try_advisory_xact_lock(canonical_lock_key)
    );
END;
$function$;

CREATE OR REPLACE FUNCTION public.b1n434_fixture_business_digest()
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
    SELECT jsonb_build_object(
        'rows', count(*),
        'sha256', encode(digest(coalesce(string_agg(concat_ws('|',
            result.user_address,
            result.positions_opened::TEXT,
            to_char(result.total_simulated_premium,
                'FM999999999999999999990.0000'),
            result.assignments::TEXT,
            to_char(result.simulated_pnl, 'FM999999999999999999990.0000'),
            to_char(result.cumulative_pnl, 'FM999999999999999999990.0000')
        ), E'\n' ORDER BY result.user_address), ''), 'sha256'), 'hex')
    )
    FROM public.user_weekly_results AS result
    WHERE result.week_start = '2026-04-03';
$function$;

CREATE OR REPLACE FUNCTION public.b1n434_fixture_query_plan()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
DECLARE
    result JSONB;
BEGIN
    EXECUTE 'EXPLAIN (FORMAT JSON, VERBOSE) SELECT
            event.id, event.user_address, event.net_premium, event.premium,
            event.is_settled, event.is_itm, event.strike_price, event.amount,
            event.is_put, event.indexed_at
        FROM public.order_events AS event
        WHERE event.indexed_at >= TIMESTAMPTZ ''2026-04-04 08:00:00+00''
          AND event.indexed_at < TIMESTAMPTZ ''2026-04-04 08:01:00+00'''
    INTO result;
    RETURN result;
END;
$function$;

GRANT SELECT, UPDATE ON public.b1n434_fixture_control TO service_role;
REVOKE ALL ON FUNCTION public.b1n434_fixture_report_guard() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1n434_fixture_set_failure(BOOLEAN) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1n434_fixture_set_hold(DOUBLE PRECISION) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1n434_fixture_state() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1n434_fixture_try_week_lock(TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1n434_fixture_business_digest() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1n434_fixture_query_plan() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.b1n434_fixture_report_guard() TO service_role;
GRANT EXECUTE ON FUNCTION public.b1n434_fixture_set_failure(BOOLEAN) TO service_role;
GRANT EXECUTE ON FUNCTION public.b1n434_fixture_set_hold(DOUBLE PRECISION) TO service_role;
GRANT EXECUTE ON FUNCTION public.b1n434_fixture_state() TO service_role;
GRANT EXECUTE ON FUNCTION public.b1n434_fixture_try_week_lock(TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.b1n434_fixture_business_digest() TO service_role;
GRANT EXECUTE ON FUNCTION public.b1n434_fixture_query_plan() TO service_role;
NOTIFY pgrst, 'reload schema';
