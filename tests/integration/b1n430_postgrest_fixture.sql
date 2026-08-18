INSERT INTO public.b1nary_accounts (id, username, username_normalized)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'scale', 'scale'),
    ('00000000-0000-0000-0000-000000000002', 'single', 'single');

INSERT INTO public.b1nary_account_members (
    account_id,
    privy_user_id,
    role,
    verified_at
) VALUES
    (
        '00000000-0000-0000-0000-000000000001',
        'did:privy:scale',
        'owner',
        now()
    ),
    (
        '00000000-0000-0000-0000-000000000002',
        'did:privy:single',
        'owner',
        now()
    );

INSERT INTO public.b1nary_wallets (
    account_id,
    privy_user_id,
    chain,
    address,
    address_normalized,
    wallet_type,
    role,
    verified_at
)
SELECT
    '00000000-0000-0000-0000-000000000001',
    'did:privy:scale',
    'base',
    '0x' || lpad(to_hex(wallet_number), 40, '0'),
    '0x' || lpad(to_hex(wallet_number), 40, '0'),
    'external',
    'trading',
    now()
FROM generate_series(1, 100) AS wallet_number;

INSERT INTO public.b1nary_wallets (
    account_id,
    privy_user_id,
    chain,
    address,
    address_normalized,
    wallet_type,
    role,
    verified_at
) VALUES (
    '00000000-0000-0000-0000-000000000002',
    'did:privy:single',
    'base',
    '0x00000000000000000000000000000000000003e9',
    '0x00000000000000000000000000000000000003e9',
    'external',
    'trading',
    now()
);

INSERT INTO public.order_events (
    id,
    tx_hash,
    block_number,
    log_index,
    chain,
    user_address,
    mm_address,
    otoken_address,
    amount,
    premium,
    gross_premium,
    net_premium,
    protocol_fee,
    collateral,
    vault_id,
    strike_price,
    expiry,
    is_put,
    is_settled,
    settled_at,
    settlement_type,
    is_itm,
    indexed_at,
    asset,
    updated_at
)
SELECT
    (
        substr(md5('b1n430-position-' || sequence_number::TEXT), 1, 8) || '-' ||
        substr(md5('b1n430-position-' || sequence_number::TEXT), 9, 4) || '-' ||
        substr(md5('b1n430-position-' || sequence_number::TEXT), 13, 4) || '-' ||
        substr(md5('b1n430-position-' || sequence_number::TEXT), 17, 4) || '-' ||
        substr(md5('b1n430-position-' || sequence_number::TEXT), 21, 12)
    )::UUID,
    '0x' || lpad(to_hex(sequence_number), 64, '0'),
    sequence_number,
    sequence_number % 100,
    'base',
    CASE
        WHEN sequence_number > 99689
            THEN '0x0000000000000000000000000000000000000001'
        ELSE '0x0000000000000000000000000000000000000002'
    END,
    '0x00000000000000000000000000000000000000aa',
    '0x' || lpad(to_hex((sequence_number % 20) + 5000), 40, '0'),
    100000000,
    1000000,
    1100000,
    1000000,
    100000,
    1000000,
    sequence_number,
    (3000 + (sequence_number % 3) * 100) * 100000000::NUMERIC,
    2000000000 + (sequence_number % 3) * 3600,
    sequence_number % 2 = 0,
    sequence_number % 5 = 0,
    CASE
        WHEN sequence_number % 5 = 0 THEN
            TIMESTAMPTZ '2026-08-02 00:00:00+00'
                + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
        ELSE NULL
    END,
    CASE WHEN sequence_number % 5 = 0 THEN 'otm' ELSE NULL END,
    CASE WHEN sequence_number % 5 = 0 THEN FALSE ELSE NULL END,
    TIMESTAMPTZ '2026-08-01 00:00:00+00'
        + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second',
    'eth',
    TIMESTAMPTZ '2026-08-01 00:00:00+00'
        + floor(sequence_number / 10)::BIGINT * INTERVAL '1 second'
FROM generate_series(1, 100000) AS sequence_number;

ANALYZE public.order_events;

CREATE OR REPLACE FUNCTION public.b1n430_fixture_legacy_position_counts(
    p_asset TEXT,
    p_chain TEXT,
    p_now BIGINT
) RETURNS TABLE (
    strike_price TEXT,
    is_put BOOLEAN,
    expiry BIGINT,
    position_count BIGINT
)
LANGUAGE sql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT
        (event.strike_price / 100000000::NUMERIC)::TEXT,
        event.is_put,
        event.expiry,
        count(*)::BIGINT
    FROM public.order_events event
    WHERE event.asset = lower(p_asset)
      AND event.chain = p_chain
      AND event.is_settled IS NOT TRUE
      AND event.expiry > p_now
    GROUP BY event.strike_price, event.is_put, event.expiry
    ORDER BY event.strike_price, event.is_put, event.expiry;
$$;

REVOKE ALL ON FUNCTION public.b1n430_fixture_legacy_position_counts(
    TEXT, TEXT, BIGINT
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.b1n430_fixture_legacy_position_counts(
    TEXT, TEXT, BIGINT
) TO service_role;

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.order_events TO service_role;
GRANT SELECT ON public.b1nary_accounts TO service_role;
GRANT SELECT, UPDATE ON public.b1nary_account_members TO service_role;
GRANT SELECT, UPDATE ON public.b1nary_wallets TO service_role;
