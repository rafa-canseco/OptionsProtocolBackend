-- Keep exactly 100k activity rows. The bulk source is anchored to UTC today
-- rather than a fixed calendar date so daysSinceFirst is deterministic.
INSERT INTO public.order_events (
    id,
    tx_hash,
    block_number,
    log_index,
    chain,
    user_address,
    otoken_address,
    amount,
    premium,
    net_premium,
    collateral,
    collateral_usd,
    vault_id,
    strike_price,
    is_put,
    indexed_at,
    asset
)
SELECT
    (
        substr(md5('b1n432-order-' || sequence_number::TEXT), 1, 8) || '-' ||
        substr(md5('b1n432-order-' || sequence_number::TEXT), 9, 4) || '-' ||
        substr(md5('b1n432-order-' || sequence_number::TEXT), 13, 4) || '-' ||
        substr(md5('b1n432-order-' || sequence_number::TEXT), 17, 4) || '-' ||
        substr(md5('b1n432-order-' || sequence_number::TEXT), 21, 12)
    )::UUID,
    '0xb1n432-order-' || sequence_number::TEXT,
    sequence_number,
    sequence_number,
    CASE WHEN sequence_number % 7 = 0 THEN 'other-chain' ELSE 'base' END,
    CASE
        WHEN sequence_number % 10 = 0
            THEN '0x0000000000000000000000000000000000000003'
        WHEN sequence_number % 2 = 0
            THEN '0x0000000000000000000000000000000000000002'
        ELSE '0x0000000000000000000000000000000000000001'
    END,
    '0x0000000000000000000000000000000000000042',
    1,
    2000000,
    CASE sequence_number % 3
        WHEN 0 THEN NULL
        WHEN 1 THEN 0
        ELSE 1000000
    END,
    CASE
        WHEN sequence_number % 2 = 1 THEN 500000
        WHEN sequence_number % 4 = 0 THEN 100000000
        ELSE 1000000000000000000
    END,
    1,
    sequence_number,
    50000000,
    CASE
        WHEN sequence_number % 5 = 0 THEN NULL
        WHEN sequence_number % 2 = 1 THEN TRUE
        ELSE FALSE
    END,
    (
        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::DATE - 2 + TIME '12:00:00'
    ) AT TIME ZONE 'UTC',
    CASE
        WHEN sequence_number % 2 = 1 THEN 'usdc'
        WHEN sequence_number % 4 = 0 THEN 'btc'
        ELSE 'eth'
    END
FROM generate_series(11, 100000) AS sequence_number;

INSERT INTO public.order_events (
    tx_hash, block_number, log_index, chain, user_address, otoken_address,
    amount, premium, net_premium, collateral, collateral_usd, vault_id,
    strike_price, is_put, indexed_at, asset
)
SELECT
    '0xb1n432-edge-' || fixture.sequence_number,
    fixture.sequence_number,
    fixture.sequence_number,
    fixture.chain,
    fixture.user_address,
    '0x0000000000000000000000000000000000000042',
    1,
    fixture.premium,
    fixture.net_premium,
    fixture.collateral,
    fixture.collateral_usd,
    fixture.sequence_number,
    fixture.strike_price,
    fixture.is_put,
    (
        (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::DATE
        + fixture.day_offset + TIME '12:00:00'
    ) AT TIME ZONE 'UTC',
    fixture.asset
FROM (VALUES
    (1, 'base', '0x0000000000000000000000000000000000000432',
        100000, 2675000, 2675000, 1.005::DOUBLE PRECISION, 0, TRUE, 'usdc', -1),
    (2, 'base', '0x0000000000000000000000000000000000000432',
        200000, 0, 1000000000000000000, 2.675::DOUBLE PRECISION,
        250000000000, FALSE, 'eth', 0),
    (3, 'other-chain', '0x0000000000000000000000000000000000000432',
        300000, NULL, 100000000, -1.005::DOUBLE PRECISION,
        9000000000000, FALSE, 'btc', 1),
    (4, 'base', '0x0000000000000000000000000000000000000432',
        999999, -100000, 2000000000000000000, 0::DOUBLE PRECISION,
        125000000, FALSE, 'other', 0),
    (5, 'base', '0x0000000000000000000000000000000000000432',
        125000, 0, -500000, 0::DOUBLE PRECISION, 0, TRUE, 'usdc', -1),
    (6, 'base', '0x0000000000000000000000000000000000000433',
        0, 0, 1000000, 1::DOUBLE PRECISION, 0, TRUE, 'usdc', 2),
    (7, 'base', '0x0000000000000000000000000000000000000433',
        0, 0, 1000000, 1::DOUBLE PRECISION, 0, TRUE, 'usdc', 2),
    (8, 'base', '0x0000000000000000000000000000000000000434',
        0, 0, 0, 0::DOUBLE PRECISION, 0, TRUE, 'usdc', 0),
    (9, 'base', '0x0000000000000000000000000000000000000434',
        0, 0, 0, 0::DOUBLE PRECISION, 0, TRUE, 'usdc', 0),
    (10, 'base', '0x0000000000000000000000000000000000000434',
        0, 0, 0, 0::DOUBLE PRECISION, 0, TRUE, 'usdc', 0)
) AS fixture(
    sequence_number, chain, user_address, premium, net_premium, collateral,
    collateral_usd, strike_price, is_put, asset, day_offset
);

INSERT INTO public.yield_distributions (
    id,
    harvest_tx_hash,
    asset,
    total_yield,
    platform_fee,
    period_start,
    period_end,
    distributed_at
) VALUES
    (
        '10000000-0000-4000-8000-000000000001',
        '0xb1n432-usdc',
        'usdc',
        3000000,
        120000,
        '2026-07-25T00:00:00Z',
        '2026-08-01T00:00:00Z',
        '2026-08-01T01:00:00Z'
    ),
    (
        '10000000-0000-4000-8000-000000000002',
        '0xb1n432-eth',
        'eth',
        3000000000000000000,
        120000000000000000,
        '2026-07-25T00:00:00Z',
        '2026-08-01T00:00:00Z',
        '2026-08-01T01:00:00Z'
    ),
    (
        '10000000-0000-4000-8000-000000000003',
        '0xb1n432-btc',
        'btc',
        300000000,
        12000000,
        '2026-07-25T00:00:00Z',
        '2026-08-01T00:00:00Z',
        '2026-08-01T01:00:00Z'
    );

INSERT INTO public.yield_positions (
    id,
    user_address,
    vault_id,
    asset,
    collateral_amount,
    deposited_at,
    settled_at,
    block_number,
    tx_hash,
    created_at,
    updated_at
)
SELECT
    (
        substr(md5('b1n432-position-' || sequence_number::TEXT), 1, 8) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 9, 4) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 13, 4) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 17, 4) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 21, 12)
    )::UUID,
    CASE WHEN sequence_number % 2 = 0
        THEN '0x0000000000000000000000000000000000000002'
        ELSE '0x0000000000000000000000000000000000000001'
    END,
    sequence_number,
    CASE sequence_number % 6
        WHEN 0 THEN 'btc'
        WHEN 3 THEN 'btc'
        WHEN 1 THEN 'eth'
        WHEN 4 THEN 'eth'
        ELSE 'usdc'
    END,
    CASE sequence_number % 6
        WHEN 0 THEN 100000000 + sequence_number
        WHEN 3 THEN 100000000 + sequence_number
        WHEN 1 THEN 1000000000000000000 + sequence_number
        WHEN 4 THEN 1000000000000000000 + sequence_number
        ELSE 1000000 + sequence_number
    END,
    CASE WHEN sequence_number % 100 < 97
        THEN TIMESTAMPTZ '2026-08-04T00:00:00Z'
            + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
        ELSE TIMESTAMPTZ '2026-07-31T00:00:00Z'
            + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
    END,
    CASE sequence_number % 100
        WHEN 97 THEN TIMESTAMPTZ '2026-07-31T12:00:00Z'
            + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
        WHEN 98 THEN TIMESTAMPTZ '2026-08-02T00:00:00Z'
            + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
        ELSE NULL
    END,
    sequence_number,
    '0xyield-position-' || sequence_number::TEXT,
    TIMESTAMPTZ '2026-08-02T00:00:00Z'
        + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second',
    TIMESTAMPTZ '2026-08-02T00:00:00Z'
        + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
FROM generate_series(1, 100000) AS sequence_number;

INSERT INTO public.yield_allocations (
    id,
    distribution_id,
    position_id,
    user_address,
    asset,
    amount,
    status,
    airdrop_tx_hash,
    created_at,
    updated_at
)
SELECT
    (
        substr(md5('b1n432-allocation-' || sequence_number::TEXT), 1, 8) || '-' ||
        substr(md5('b1n432-allocation-' || sequence_number::TEXT), 9, 4) || '-' ||
        substr(md5('b1n432-allocation-' || sequence_number::TEXT), 13, 4) || '-' ||
        substr(md5('b1n432-allocation-' || sequence_number::TEXT), 17, 4) || '-' ||
        substr(md5('b1n432-allocation-' || sequence_number::TEXT), 21, 12)
    )::UUID,
    CASE sequence_number % 6
        WHEN 0 THEN '10000000-0000-4000-8000-000000000003'::UUID
        WHEN 3 THEN '10000000-0000-4000-8000-000000000003'::UUID
        WHEN 1 THEN '10000000-0000-4000-8000-000000000002'::UUID
        WHEN 4 THEN '10000000-0000-4000-8000-000000000002'::UUID
        ELSE '10000000-0000-4000-8000-000000000001'::UUID
    END,
    (
        substr(md5('b1n432-position-' || sequence_number::TEXT), 1, 8) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 9, 4) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 13, 4) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 17, 4) || '-' ||
        substr(md5('b1n432-position-' || sequence_number::TEXT), 21, 12)
    )::UUID,
    '0x0000000000000000000000000000000000000002',
    CASE sequence_number % 6
        WHEN 0 THEN 'btc'
        WHEN 3 THEN 'btc'
        WHEN 1 THEN 'eth'
        WHEN 4 THEN 'eth'
        ELSE 'usdc'
    END,
    1000 + sequence_number,
    CASE WHEN sequence_number % 2 = 0 THEN 'delivered' ELSE 'pending' END,
    CASE WHEN sequence_number % 2 = 0
        THEN '0xyield-allocation-' || sequence_number::TEXT
        ELSE NULL
    END,
    TIMESTAMPTZ '2026-08-02T12:00:00Z'
        + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second',
    TIMESTAMPTZ '2026-08-02T12:00:00Z'
        + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
FROM generate_series(1, 100000) AS sequence_number;

ANALYZE public.order_events;
ANALYZE public.yield_positions;
ANALYZE public.yield_allocations;
ANALYZE public.yield_distributions;

GRANT SELECT ON public.order_events TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.yield_positions TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.yield_allocations TO service_role;
GRANT SELECT, INSERT, DELETE ON public.yield_distributions TO service_role;

CREATE OR REPLACE FUNCTION public.b1n432_fixture_index_plan(
    p_kind TEXT
) RETURNS SETOF TEXT
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_kind = 'history' THEN
        RETURN QUERY EXECUTE $plan$
            EXPLAIN (COSTS OFF)
            SELECT allocation.id
            FROM public.yield_allocations AS allocation
            WHERE allocation.user_address =
                '0x0000000000000000000000000000000000000002'
              AND allocation.created_at <= '2026-08-03T18:00:00Z'::TIMESTAMPTZ
              AND (allocation.created_at, allocation.id) < (
                  '2026-08-03T17:59:00Z'::TIMESTAMPTZ,
                  'ffffffff-ffff-ffff-ffff-ffffffffffff'::UUID
              )
            ORDER BY allocation.created_at DESC, allocation.id DESC
            LIMIT 101
        $plan$;
    ELSIF p_kind = 'positions' THEN
        RETURN QUERY EXECUTE $plan$
            EXPLAIN (COSTS OFF)
            SELECT
                position.id,
                position.vault_id,
                position.asset,
                position.collateral_amount,
                position.deposited_at,
                position.settled_at
            FROM public.yield_positions AS position
            WHERE position.user_address =
                '0x0000000000000000000000000000000000000002'
              AND position.asset IN ('usdc', 'eth', 'btc')
              AND position.created_at <= '2026-08-03T18:00:00Z'::TIMESTAMPTZ
              AND (position.deposited_at, position.id) < (
                  '2026-08-05T00:00:00Z'::TIMESTAMPTZ,
                  'ffffffff-ffff-ffff-ffff-ffffffffffff'::UUID
              )
            ORDER BY position.deposited_at DESC, position.id DESC
            LIMIT 101
        $plan$;
    ELSIF p_kind = 'overlap' THEN
        RETURN QUERY EXECUTE $plan$
            EXPLAIN (COSTS OFF)
            WITH overlap_positions AS (
                SELECT position.asset, position.collateral_amount
                FROM public.yield_positions AS position
                WHERE position.settled_at IS NULL
                  AND position.deposited_at
                      < '2026-08-03T18:00:00Z'::TIMESTAMPTZ
                  AND '2026-08-03T18:00:00Z'::TIMESTAMPTZ > greatest(
                      position.deposited_at,
                      '2026-08-01T00:00:00Z'::TIMESTAMPTZ
                  )
                UNION ALL
                SELECT position.asset, position.collateral_amount
                FROM public.yield_positions AS position
                WHERE position.settled_at IS NOT NULL
                  AND position.settled_at
                      > '2026-08-01T00:00:00Z'::TIMESTAMPTZ
                  AND position.deposited_at
                      < '2026-08-03T18:00:00Z'::TIMESTAMPTZ
                  AND least(
                      position.settled_at,
                      '2026-08-03T18:00:00Z'::TIMESTAMPTZ
                  ) > greatest(
                      position.deposited_at,
                      '2026-08-01T00:00:00Z'::TIMESTAMPTZ
                  )
            )
            SELECT asset, sum(collateral_amount)
            FROM overlap_positions
            GROUP BY asset
        $plan$;
    ELSE
        RAISE EXCEPTION 'unknown plan kind';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.b1n432_fixture_index_plan(TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.b1n432_fixture_index_plan(TEXT)
    TO service_role;
