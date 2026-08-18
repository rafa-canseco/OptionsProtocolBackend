-- Exact 100k source rows, all inside the default competition window. The
-- first 100 rows are later mutated into the small semantic golden matrix.
INSERT INTO public.order_events (
    id,
    tx_hash,
    block_number,
    user_address,
    collateral_usd,
    premium,
    net_premium,
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
        substr(md5('b1n431-event-' || sequence_number::TEXT), 1, 8) || '-' ||
        substr(md5('b1n431-event-' || sequence_number::TEXT), 9, 4) || '-' ||
        substr(md5('b1n431-event-' || sequence_number::TEXT), 13, 4) || '-' ||
        substr(md5('b1n431-event-' || sequence_number::TEXT), 17, 4) || '-' ||
        substr(md5('b1n431-event-' || sequence_number::TEXT), 21, 12)
    )::UUID,
    '0x' || lpad(to_hex(sequence_number), 64, '0'),
    sequence_number,
    CASE
        -- Keep one >10k-row wallet while creating >100 total participants so
        -- real top-N truncation and large-wallet aggregation are both proved.
        WHEN sequence_number <= 80000 THEN
            '0x0000000000000000000000000000000000000002'
        ELSE '0x' || lpad((1000 + sequence_number % 150)::TEXT, 40, '0')
    END,
    1,
    100,
    100,
    sequence_number % 2 = 0,
    'eth',
    TIMESTAMPTZ '2026-03-30 00:00:00+00'
        + (sequence_number % 100000) * INTERVAL '1 second',
    extract(epoch FROM TIMESTAMPTZ '2026-04-02 23:59:59+00')::BIGINT,
    sequence_number % 5 = 0,
    CASE WHEN sequence_number % 5 = 0 THEN FALSE ELSE NULL END,
    CASE
        WHEN sequence_number % 5 = 0 THEN
            TIMESTAMPTZ '2026-03-31 00:00:00+00'
                + (sequence_number % 80000) * INTERVAL '1 second'
        ELSE NULL
    END
FROM generate_series(1, 100000) AS sequence_number;

-- Twenty already-settled rows become a compact golden matrix. Their wallet
-- addresses remain among the 100 verified B1N-430 account wallets.
WITH golden(
    block_number,
    user_address,
    collateral_usd,
    net_premium,
    is_put,
    asset,
    indexed_at,
    expiry,
    is_itm,
    settled_at
) AS (
    VALUES
        -- Completed same-asset Wheel: both legs receive 1.5x, never stacked.
        (5,  '0x0000000000000000000000000000000000000003', 250::NUMERIC, 100000::NUMERIC, TRUE,  'eth', TIMESTAMPTZ '2026-04-01 08:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-02 23:59:59+00')::BIGINT, TRUE,  TIMESTAMPTZ '2026-04-01 10:00:00+00'),
        (10, '0x0000000000000000000000000000000000000003', 250::NUMERIC, 100000::NUMERIC, FALSE, 'eth', TIMESTAMPTZ '2026-04-01 20:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-05 23:59:59+00')::BIGINT, TRUE,  TIMESTAMPTZ '2026-04-05 10:00:00+00'),

        -- Perfect Week coverage in both fixed weeks; max OTM streak is three.
        (15, '0x0000000000000000000000000000000000000004', 200::NUMERIC, 100000::NUMERIC, TRUE,  'eth', TIMESTAMPTZ '2026-04-02 08:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-02 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-03 08:00:00+00'),
        (20, '0x0000000000000000000000000000000000000004', 200::NUMERIC, 100000::NUMERIC, TRUE,  'eth', TIMESTAMPTZ '2026-04-07 08:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-07 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-09 08:00:00+00'),
        (25, '0x0000000000000000000000000000000000000004', 200::NUMERIC, 100000::NUMERIC, TRUE,  'eth', TIMESTAMPTZ '2026-04-08 08:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-08 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-10 08:00:00+00'),

        -- 3 OTM -> ITM -> 2 OTM, with one ITM suppressing Perfect Week 1.
        (30, '0x0000000000000000000000000000000000000005', 100::NUMERIC, 50000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-01 00:10:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-01 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-01 01:00:00+00'),
        (35, '0x0000000000000000000000000000000000000005', 100::NUMERIC, 50000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-01 00:20:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-01 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-01 02:00:00+00'),
        (40, '0x0000000000000000000000000000000000000005', 100::NUMERIC, 50000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-01 00:30:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-01 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-01 03:00:00+00'),
        (45, '0x0000000000000000000000000000000000000005', 100::NUMERIC, 50000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-02 00:10:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-02 23:59:59+00')::BIGINT, TRUE,  TIMESTAMPTZ '2026-04-02 01:00:00+00'),
        (50, '0x0000000000000000000000000000000000000005', 100::NUMERIC, 50000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-03 00:10:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-03 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-03 01:00:00+00'),
        (55, '0x0000000000000000000000000000000000000005', 100::NUMERIC, 50000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-03 00:20:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-03 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-03 02:00:00+00'),

        -- Exact-$500 single-position qualifiers with a deterministic address tie.
        (60, '0x0000000000000000000000000000000000000006', 500::NUMERIC, 100000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-04 08:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-04 23:59:59+00')::BIGINT, TRUE, TIMESTAMPTZ '2026-04-04 09:00:00+00'),
        (65, '0x0000000000000000000000000000000000000007', 500::NUMERIC, 100000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-05 08:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-05 23:59:59+00')::BIGINT, TRUE, TIMESTAMPTZ '2026-04-05 09:00:00+00'),

        -- Raw 499.996 displays as 500.00 but remains below qualification.
        (70, '0x0000000000000000000000000000000000000008', 499.996::DOUBLE PRECISION, 1000000::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-04-06 08:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-06 23:59:59+00')::BIGINT, TRUE, TIMESTAMPTZ '2026-04-06 09:00:00+00'),

        -- Cross-asset and half-completed Wheel candidates must not pair.
        (75, '0x0000000000000000000000000000000000000009', 125::NUMERIC, 100000::NUMERIC, TRUE,  'eth', TIMESTAMPTZ '2026-04-01 07:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-01 23:59:59+00')::BIGINT, TRUE,  TIMESTAMPTZ '2026-04-01 08:00:00+00'),
        (80, '0x0000000000000000000000000000000000000009', 125::NUMERIC, 100000::NUMERIC, FALSE, 'btc', TIMESTAMPTZ '2026-04-01 12:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-01 23:59:59+00')::BIGINT, TRUE,  TIMESTAMPTZ '2026-04-02 08:00:00+00'),
        (85, '0x0000000000000000000000000000000000000009', 125::NUMERIC, 100000::NUMERIC, TRUE,  'eth', TIMESTAMPTZ '2026-04-03 07:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-03 23:59:59+00')::BIGINT, TRUE,  TIMESTAMPTZ '2026-04-03 08:00:00+00'),
        (90, '0x0000000000000000000000000000000000000009', 125::NUMERIC, 100000::NUMERIC, FALSE, 'eth', TIMESTAMPTZ '2026-04-03 12:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-04-03 23:59:59+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-04 08:00:00+00'),

        -- Inclusive competition boundaries plus premium fallback semantics.
        -- Week-2 ITM suppresses Perfect Week, null net uses premium, and zero
        -- net remains zero rather than falling through to gross premium.
        (95,  '0x0000000000000000000000000000000000000010', 250::NUMERIC, NULL::NUMERIC, TRUE, 'eth', TIMESTAMPTZ '2026-03-30 00:00:00+00', extract(epoch FROM TIMESTAMPTZ '2026-03-30 00:00:00+00')::BIGINT, FALSE, TIMESTAMPTZ '2026-04-12 23:59:58+00'),
        (100, '0x0000000000000000000000000000000000000010', 250::NUMERIC, 0::NUMERIC,    TRUE, 'eth', TIMESTAMPTZ '2026-04-12 23:59:59+00', extract(epoch FROM TIMESTAMPTZ '2026-04-12 23:59:59+00')::BIGINT, TRUE,  TIMESTAMPTZ '2026-04-12 23:59:59+00')
)
UPDATE public.order_events AS event
SET
    user_address = golden.user_address,
    collateral_usd = golden.collateral_usd,
    premium = CASE event.block_number
        WHEN 95 THEN 200000
        WHEN 100 THEN 900000
        ELSE golden.net_premium
    END,
    gross_premium = CASE event.block_number
        WHEN 95 THEN 200000
        WHEN 100 THEN 900000
        ELSE golden.net_premium
    END,
    net_premium = golden.net_premium,
    is_put = golden.is_put,
    asset = golden.asset,
    indexed_at = golden.indexed_at,
    expiry = golden.expiry,
    is_settled = TRUE,
    is_itm = golden.is_itm,
    settled_at = golden.settled_at
FROM golden
WHERE event.block_number = golden.block_number;

-- One hundred distinct sub-cent wallets isolate sum-then-round behavior:
-- each public wallet total is 0.00, while their raw aggregate rounds to 0.40.
UPDATE public.order_events AS event
SET
    user_address = '0x' || lpad((2000 + event.block_number)::TEXT, 40, '0'),
    collateral_usd = 0.004::DOUBLE PRECISION,
    premium = 0,
    net_premium = 0,
    indexed_at = TIMESTAMPTZ '2026-04-11 12:00:00+00'
        + event.block_number * INTERVAL '1 second',
    expiry = extract(epoch FROM TIMESTAMPTZ '2026-04-11 23:59:59+00')::BIGINT,
    is_settled = FALSE,
    is_itm = NULL,
    settled_at = NULL
WHERE event.block_number BETWEEN 101 AND 200;

-- Exact DOUBLE PRECISION half-cent values exercise predecessor Python round
-- through both backend routes. These are intentionally not pre-rounded in SQL.
WITH rounding(block_number, user_address, collateral_usd, indexed_at) AS (
    VALUES
        (201, '0x0000000000000000000000000000000000003001', 2.675::DOUBLE PRECISION, TIMESTAMPTZ '2026-04-11 13:00:10+00'),
        (202, '0x0000000000000000000000000000000000003002', 2.685::DOUBLE PRECISION, TIMESTAMPTZ '2026-04-11 13:00:20+00'),
        (203, '0x0000000000000000000000000000000000003003', 2.665::DOUBLE PRECISION, TIMESTAMPTZ '2026-04-11 13:00:30+00'),
        (204, '0x0000000000000000000000000000000000003004', 2.655::DOUBLE PRECISION, TIMESTAMPTZ '2026-04-11 13:00:40+00')
)
UPDATE public.order_events AS event
SET
    user_address = rounding.user_address,
    collateral_usd = rounding.collateral_usd,
    premium = 0,
    net_premium = 0,
    indexed_at = rounding.indexed_at,
    expiry = extract(epoch FROM TIMESTAMPTZ '2026-04-12 13:00:00+00')::BIGINT,
    is_settled = FALSE,
    is_itm = NULL,
    settled_at = NULL
FROM rounding
WHERE event.block_number = rounding.block_number;

-- SQL-side predecessor rounding regressions. Keep the progress boundary, the
-- seven-base-unit Perfect Week rank tie, and the six-decimal earning-rate rank
-- tie independently queryable.
WITH predecessor_rounding(
    block_number,
    user_address,
    collateral_usd,
    net_premium,
    indexed_at,
    is_itm,
    settled_at
) AS (
    VALUES
        (205, '0x0000000000000000000000000000000000004004', 0.075::DOUBLE PRECISION, 0::NUMERIC, TIMESTAMPTZ '2026-04-11 14:00:10+00', NULL, NULL),
        (206, '0x0000000000000000000000000000000000004001', 501::DOUBLE PRECISION, 0::NUMERIC, TIMESTAMPTZ '2026-04-11 14:00:20+00', TRUE, TIMESTAMPTZ '2026-04-04 12:00:01+00'),
        (207, '0x0000000000000000000000000000000000004003', 501::DOUBLE PRECISION, 7::NUMERIC, TIMESTAMPTZ '2026-04-11 14:00:30+00', FALSE, TIMESTAMPTZ '2026-04-04 12:00:02+00'),
        (208, '0x0000000000000000000000000000000000005001', 501::DOUBLE PRECISION, 0::NUMERIC, TIMESTAMPTZ '2026-04-11 14:00:40+00', TRUE, TIMESTAMPTZ '2026-04-04 12:00:03+00'),
        (209, '0x0000000000000000000000000000000000005002', 501::DOUBLE PRECISION, 167::NUMERIC, TIMESTAMPTZ '2026-04-11 14:00:50+00', FALSE, TIMESTAMPTZ '2026-04-04 12:00:04+00')
)
UPDATE public.order_events AS event
SET
    user_address = predecessor_rounding.user_address,
    collateral_usd = predecessor_rounding.collateral_usd,
    premium = predecessor_rounding.net_premium,
    net_premium = predecessor_rounding.net_premium,
    indexed_at = predecessor_rounding.indexed_at,
    expiry = extract(epoch FROM TIMESTAMPTZ '2026-04-12 14:00:00+00')::BIGINT,
    is_settled = TRUE,
    is_itm = predecessor_rounding.is_itm,
    settled_at = predecessor_rounding.settled_at
FROM predecessor_rounding
WHERE event.block_number = predecessor_rounding.block_number;

ANALYZE public.order_events;

-- Fixture-only public bridge for a broad Python oracle comparison. Production
-- keeps the exact-binary helper in the non-exposed private schema.
CREATE OR REPLACE FUNCTION public.b1n431_fixture_python_round(p_cases JSONB)
RETURNS JSONB
LANGUAGE sql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
    SELECT coalesce(
        jsonb_agg(
            private.b1n431_python_round(
                (item.value ->> 'value')::DOUBLE PRECISION,
                (item.value ->> 'ndigits')::INTEGER
            ) ORDER BY item.ordinality
        ),
        '[]'::JSONB
    )
    FROM jsonb_array_elements(p_cases) WITH ORDINALITY AS item(value, ordinality);
$function$;

REVOKE ALL ON FUNCTION public.b1n431_fixture_python_round(JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.b1n431_fixture_python_round(JSONB) TO service_role;

CREATE OR REPLACE FUNCTION public.b1n431_fixture_query_plans()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
DECLARE
    global_plan JSONB;
    wallet_plan JSONB;
BEGIN
    -- Match the leaderboard RPC's source projection and per-wallet JSON
    -- aggregation so the plan must fetch real heap fields, not only an id.
    EXECUTE 'EXPLAIN (FORMAT JSON, VERBOSE) SELECT
            lower(e.user_address) AS wallet,
            jsonb_agg(jsonb_build_object(
                ''id'', e.id,
                ''collateral_usd'', e.collateral_usd,
                ''net_premium'', e.net_premium,
                ''premium'', e.premium,
                ''is_put'', e.is_put,
                ''asset'', e.asset,
                ''indexed_at'', e.indexed_at,
                ''expiry'', e.expiry,
                ''is_itm'', e.is_itm,
                ''settled_at'', e.settled_at
            ) ORDER BY e.indexed_at, e.id)
        FROM public.order_events AS e
        WHERE e.indexed_at >= TIMESTAMPTZ ''2026-04-01 00:00:00+00''
          AND e.indexed_at <= TIMESTAMPTZ ''2026-04-12 23:59:59+00''
          AND lower(e.user_address) <> ''''
        GROUP BY lower(e.user_address)'
    INTO global_plan;
    -- Match the /me RPC projection and fixed-cardinality JSON aggregation.
    EXECUTE 'EXPLAIN (FORMAT JSON, VERBOSE) SELECT jsonb_agg(
            jsonb_build_object(
                ''id'', e.id,
                ''collateral_usd'', e.collateral_usd,
                ''net_premium'', e.net_premium,
                ''premium'', e.premium,
                ''is_put'', e.is_put,
                ''asset'', e.asset,
                ''indexed_at'', e.indexed_at,
                ''expiry'', e.expiry,
                ''is_itm'', e.is_itm,
                ''settled_at'', e.settled_at
            ) ORDER BY e.indexed_at, e.id
        )
        FROM public.order_events AS e
        WHERE e.user_address = ''0x0000000000000000000000000000000000000006''
          AND e.indexed_at >= TIMESTAMPTZ ''2026-04-01 00:00:00+00''
          AND e.indexed_at <= TIMESTAMPTZ ''2026-04-12 23:59:59+00'''
    INTO wallet_plan;
    RETURN jsonb_build_object('global', global_plan, 'wallet', wallet_plan);
END;
$function$;

REVOKE ALL ON FUNCTION public.b1n431_fixture_query_plans()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.b1n431_fixture_query_plans() TO service_role;
