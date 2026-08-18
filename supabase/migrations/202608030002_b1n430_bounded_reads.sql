-- B1N-430: bounded portfolio reads and bounded price-count aggregation.

ALTER TABLE public.order_events
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION public.b1n430_touch_order_event_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS b1n430_order_events_updated_at ON public.order_events;
CREATE TRIGGER b1n430_order_events_updated_at
BEFORE UPDATE ON public.order_events
FOR EACH ROW EXECUTE FUNCTION public.b1n430_touch_order_event_updated_at();

CREATE INDEX IF NOT EXISTS idx_b1n430_order_events_active_page
    ON public.order_events (user_address, chain, indexed_at DESC, id DESC)
    WHERE is_settled IS NOT TRUE;
CREATE INDEX IF NOT EXISTS idx_b1n430_order_events_settled_page
    ON public.order_events (
        user_address,
        chain,
        (coalesce(settled_at, updated_at)) DESC,
        id DESC
    )
    WHERE is_settled IS TRUE;
CREATE INDEX IF NOT EXISTS idx_b1n430_order_events_changes
    ON public.order_events (user_address, chain, updated_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_b1n430_order_events_active_counts
    ON public.order_events (asset, chain, expiry, strike_price, is_put)
    WHERE is_settled IS NOT TRUE;

CREATE OR REPLACE FUNCTION public.b1nary_position_page(
    p_account_id UUID DEFAULT NULL,
    p_privy_user_id TEXT DEFAULT NULL,
    p_wallets JSONB DEFAULT NULL,
    p_stream TEXT DEFAULT 'snapshot',
    p_limit INTEGER DEFAULT 50,
    p_active_limit INTEGER DEFAULT 50,
    p_settled_limit INTEGER DEFAULT 20,
    p_cursor_at TIMESTAMPTZ DEFAULT NULL,
    p_cursor_id UUID DEFAULT NULL,
    p_changed_after TIMESTAMPTZ DEFAULT NULL,
    p_watermark TIMESTAMPTZ DEFAULT NULL,
    p_wallet_fingerprint TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    identity_count INTEGER;
    identity_found BOOLEAN := TRUE;
    resolved_wallets JSONB := '[]'::JSONB;
    resolved_wallet_fingerprint TEXT;
    effective_watermark TIMESTAMPTZ := coalesce(p_watermark, statement_timestamp());
    page_rows JSONB := '[]'::JSONB;
    active_rows JSONB := '[]'::JSONB;
    settled_rows JSONB := '[]'::JSONB;
BEGIN
    identity_count := (p_account_id IS NOT NULL)::INTEGER
        + (p_privy_user_id IS NOT NULL)::INTEGER
        + (p_wallets IS NOT NULL)::INTEGER;
    IF identity_count <> 1 THEN
        RAISE EXCEPTION 'exactly one position identity is required';
    END IF;
    IF p_stream NOT IN ('snapshot', 'active', 'settled', 'changes') THEN
        RAISE EXCEPTION 'invalid position stream';
    END IF;
    IF p_limit < 1 OR p_limit > 100
        OR p_active_limit < 1 OR p_active_limit > 100
        OR p_settled_limit < 1 OR p_settled_limit > 100 THEN
        RAISE EXCEPTION 'position limits must be between 1 and 100';
    END IF;
    IF (p_cursor_at IS NULL) <> (p_cursor_id IS NULL) THEN
        RAISE EXCEPTION 'both cursor components are required';
    END IF;
    IF p_stream = 'changes' AND p_changed_after IS NULL THEN
        RAISE EXCEPTION 'changed_after is required for changes';
    END IF;

    IF p_account_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1 FROM public.b1nary_accounts a WHERE a.id = p_account_id
        ) INTO identity_found;
        SELECT coalesce(
            jsonb_agg(
                jsonb_build_object('chain', w.chain, 'address', w.address_normalized)
                ORDER BY w.chain, w.address_normalized
            ),
            '[]'::JSONB
        )
        INTO resolved_wallets
        FROM public.b1nary_wallets w
        WHERE w.account_id = p_account_id
          AND w.role = 'trading'
          AND w.verified_at IS NOT NULL;
    ELSIF p_privy_user_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM public.b1nary_account_members m
            WHERE m.privy_user_id = p_privy_user_id
        ) INTO identity_found;
        SELECT coalesce(
            jsonb_agg(
                DISTINCT jsonb_build_object(
                    'chain', w.chain,
                    'address', w.address_normalized
                )
            ),
            '[]'::JSONB
        )
        INTO resolved_wallets
        FROM public.b1nary_account_members m
        JOIN public.b1nary_wallets w ON w.account_id = m.account_id
        WHERE m.privy_user_id = p_privy_user_id
          AND w.role = 'trading'
          AND w.verified_at IS NOT NULL;
    ELSE
        IF jsonb_typeof(p_wallets) <> 'array'
            OR jsonb_array_length(p_wallets) < 1
            OR jsonb_array_length(p_wallets) > 100 THEN
            RAISE EXCEPTION 'wallet list must contain between 1 and 100 entries';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM jsonb_to_recordset(p_wallets) AS wallet(chain TEXT, address TEXT)
            WHERE wallet.chain NOT IN ('base', 'solana')
               OR nullif(wallet.address, '') IS NULL
        ) THEN
            RAISE EXCEPTION 'invalid wallet filter';
        END IF;
        SELECT coalesce(
            jsonb_agg(
                DISTINCT jsonb_build_object(
                    'chain', wallet.chain,
                    'address', wallet.address
                )
            ),
            '[]'::JSONB
        )
        INTO resolved_wallets
        FROM jsonb_to_recordset(p_wallets) AS wallet(chain TEXT, address TEXT);
    END IF;

    SELECT encode(
        sha256(
            convert_to(
                coalesce(
                    string_agg(
                        canonical_wallet.chain || ':'
                            || length(canonical_wallet.address)::TEXT || ':'
                            || canonical_wallet.address,
                        chr(30)
                        ORDER BY canonical_wallet.chain, canonical_wallet.address
                    ),
                    ''
                ),
                'UTF8'
            )
        ),
        'hex'
    )
    INTO resolved_wallet_fingerprint
    FROM (
        SELECT DISTINCT wallet.chain, wallet.address
        FROM jsonb_to_recordset(resolved_wallets)
            AS wallet(chain TEXT, address TEXT)
    ) canonical_wallet;

    IF p_wallet_fingerprint IS NOT NULL
        AND p_wallet_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid wallet fingerprint';
    END IF;
    IF p_wallet_fingerprint IS NOT NULL
        AND p_wallet_fingerprint <> resolved_wallet_fingerprint THEN
        RETURN jsonb_build_object(
            'account_found', identity_found,
            'filter_mismatch', TRUE,
            'wallet_fingerprint', resolved_wallet_fingerprint,
            'watermark', effective_watermark,
            'active', '[]'::JSONB,
            'settled', '[]'::JSONB,
            'rows', '[]'::JSONB
        );
    END IF;

    IF NOT identity_found THEN
        RETURN jsonb_build_object(
            'account_found', FALSE,
            'wallet_fingerprint', resolved_wallet_fingerprint,
            'watermark', effective_watermark,
            'active', '[]'::JSONB,
            'settled', '[]'::JSONB,
            'rows', '[]'::JSONB
        );
    END IF;

    IF p_stream = 'snapshot' THEN
        SELECT coalesce(
            jsonb_agg(to_jsonb(page_row) ORDER BY page_row.indexed_at DESC, page_row.id DESC),
            '[]'::JSONB
        )
        INTO active_rows
        FROM (
            SELECT
                e.id, e.tx_hash, e.block_number, e.chain, e.user_address,
                e.mm_address, e.otoken_address, e.amount, e.premium,
                e.gross_premium, e.net_premium, e.protocol_fee, e.collateral,
                e.vault_id, e.strike_price, e.expiry, e.is_put, e.is_settled,
                e.settled_at, e.settlement_tx_hash, e.settlement_type, e.is_itm,
                e.expiry_price, e.delivered_asset, e.delivered_amount,
                e.delivery_tx_hash, e.group_id, e.indexed_at, e.asset, e.updated_at
            FROM public.order_events e
            JOIN jsonb_to_recordset(resolved_wallets)
                AS wallet(chain TEXT, address TEXT)
              ON wallet.chain = e.chain AND wallet.address = e.user_address
            WHERE e.is_settled IS NOT TRUE
              AND e.updated_at <= effective_watermark
            ORDER BY e.indexed_at DESC, e.id DESC
            LIMIT p_active_limit + 1
        ) page_row;

        SELECT coalesce(
            jsonb_agg(
                to_jsonb(page_row) - 'settled_sort_at'
                ORDER BY page_row.settled_sort_at DESC, page_row.id DESC
            ),
            '[]'::JSONB
        )
        INTO settled_rows
        FROM (
            SELECT
                e.id, e.tx_hash, e.block_number, e.chain, e.user_address,
                e.mm_address, e.otoken_address, e.amount, e.premium,
                e.gross_premium, e.net_premium, e.protocol_fee, e.collateral,
                e.vault_id, e.strike_price, e.expiry, e.is_put, e.is_settled,
                e.settled_at, e.settlement_tx_hash, e.settlement_type, e.is_itm,
                e.expiry_price, e.delivered_asset, e.delivered_amount,
                e.delivery_tx_hash, e.group_id, e.indexed_at, e.asset, e.updated_at,
                coalesce(e.settled_at, e.updated_at) AS settled_sort_at
            FROM public.order_events e
            JOIN jsonb_to_recordset(resolved_wallets)
                AS wallet(chain TEXT, address TEXT)
              ON wallet.chain = e.chain AND wallet.address = e.user_address
            WHERE e.is_settled IS TRUE
              AND e.updated_at <= effective_watermark
            ORDER BY coalesce(e.settled_at, e.updated_at) DESC, e.id DESC
            LIMIT p_settled_limit + 1
        ) page_row;

        RETURN jsonb_build_object(
            'account_found', TRUE,
            'wallet_fingerprint', resolved_wallet_fingerprint,
            'watermark', effective_watermark,
            'active', active_rows,
            'settled', settled_rows
        );
    END IF;

    IF p_stream = 'active' THEN
        SELECT coalesce(
            jsonb_agg(to_jsonb(page_row) ORDER BY page_row.indexed_at DESC, page_row.id DESC),
            '[]'::JSONB
        )
        INTO page_rows
        FROM (
            SELECT
                e.id, e.tx_hash, e.block_number, e.chain, e.user_address,
                e.mm_address, e.otoken_address, e.amount, e.premium,
                e.gross_premium, e.net_premium, e.protocol_fee, e.collateral,
                e.vault_id, e.strike_price, e.expiry, e.is_put, e.is_settled,
                e.settled_at, e.settlement_tx_hash, e.settlement_type, e.is_itm,
                e.expiry_price, e.delivered_asset, e.delivered_amount,
                e.delivery_tx_hash, e.group_id, e.indexed_at, e.asset, e.updated_at
            FROM public.order_events e
            JOIN jsonb_to_recordset(resolved_wallets)
                AS wallet(chain TEXT, address TEXT)
              ON wallet.chain = e.chain AND wallet.address = e.user_address
            WHERE e.is_settled IS NOT TRUE
              AND e.updated_at <= effective_watermark
              AND (
                  p_cursor_at IS NULL
                  OR (e.indexed_at, e.id) < (p_cursor_at, p_cursor_id)
              )
            ORDER BY e.indexed_at DESC, e.id DESC
            LIMIT p_limit + 1
        ) page_row;
    ELSIF p_stream = 'settled' THEN
        SELECT coalesce(
            jsonb_agg(
                to_jsonb(page_row) - 'settled_sort_at'
                ORDER BY page_row.settled_sort_at DESC, page_row.id DESC
            ),
            '[]'::JSONB
        )
        INTO page_rows
        FROM (
            SELECT
                e.id, e.tx_hash, e.block_number, e.chain, e.user_address,
                e.mm_address, e.otoken_address, e.amount, e.premium,
                e.gross_premium, e.net_premium, e.protocol_fee, e.collateral,
                e.vault_id, e.strike_price, e.expiry, e.is_put, e.is_settled,
                e.settled_at, e.settlement_tx_hash, e.settlement_type, e.is_itm,
                e.expiry_price, e.delivered_asset, e.delivered_amount,
                e.delivery_tx_hash, e.group_id, e.indexed_at, e.asset, e.updated_at,
                coalesce(e.settled_at, e.updated_at) AS settled_sort_at
            FROM public.order_events e
            JOIN jsonb_to_recordset(resolved_wallets)
                AS wallet(chain TEXT, address TEXT)
              ON wallet.chain = e.chain AND wallet.address = e.user_address
            WHERE e.is_settled IS TRUE
              AND e.updated_at <= effective_watermark
              AND (
                  p_cursor_at IS NULL
                  OR (coalesce(e.settled_at, e.updated_at), e.id)
                      < (p_cursor_at, p_cursor_id)
              )
            ORDER BY coalesce(e.settled_at, e.updated_at) DESC, e.id DESC
            LIMIT p_limit + 1
        ) page_row;
    ELSE
        SELECT coalesce(
            jsonb_agg(to_jsonb(page_row) ORDER BY page_row.updated_at ASC, page_row.id ASC),
            '[]'::JSONB
        )
        INTO page_rows
        FROM (
            SELECT
                e.id, e.tx_hash, e.block_number, e.chain, e.user_address,
                e.mm_address, e.otoken_address, e.amount, e.premium,
                e.gross_premium, e.net_premium, e.protocol_fee, e.collateral,
                e.vault_id, e.strike_price, e.expiry, e.is_put, e.is_settled,
                e.settled_at, e.settlement_tx_hash, e.settlement_type, e.is_itm,
                e.expiry_price, e.delivered_asset, e.delivered_amount,
                e.delivery_tx_hash, e.group_id, e.indexed_at, e.asset, e.updated_at
            FROM public.order_events e
            JOIN jsonb_to_recordset(resolved_wallets)
                AS wallet(chain TEXT, address TEXT)
              ON wallet.chain = e.chain AND wallet.address = e.user_address
            WHERE e.updated_at > p_changed_after
              AND e.updated_at <= effective_watermark
              AND (
                  p_cursor_at IS NULL
                  OR (e.updated_at, e.id) > (p_cursor_at, p_cursor_id)
              )
            ORDER BY e.updated_at ASC, e.id ASC
            LIMIT p_limit + 1
        ) page_row;
    END IF;

    RETURN jsonb_build_object(
        'account_found', TRUE,
        'wallet_fingerprint', resolved_wallet_fingerprint,
        'watermark', effective_watermark,
        'rows', page_rows
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.b1nary_price_position_counts(
    p_asset TEXT,
    p_chain TEXT,
    p_now BIGINT,
    p_series JSONB
) RETURNS TABLE (
    strike_price TEXT,
    is_put BOOLEAN,
    expiry BIGINT,
    position_count BIGINT
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_chain NOT IN ('base', 'solana') THEN
        RAISE EXCEPTION 'invalid chain';
    END IF;
    IF p_series IS NULL OR jsonb_typeof(p_series) <> 'array'
        OR jsonb_array_length(p_series) > 100 THEN
        RAISE EXCEPTION 'series must be an array with at most 100 entries';
    END IF;

    RETURN QUERY
    WITH visible AS (
        SELECT
            series.ordinality,
            (series.item ->> 'strike_price')::NUMERIC AS strike_usd,
            (series.item ->> 'is_put')::BOOLEAN AS option_is_put,
            (series.item ->> 'expiry')::BIGINT AS option_expiry,
            (series.item ->> 'lower_expiry')::BIGINT AS lower_expiry,
            (series.item ->> 'upper_expiry')::BIGINT AS upper_expiry
        FROM jsonb_array_elements(p_series) WITH ORDINALITY AS series(item, ordinality)
    ),
    mapped AS (
        SELECT
            visible.ordinality,
            count(*)::BIGINT AS mapped_count
        FROM visible
        JOIN public.order_events e
          ON e.strike_price / 100000000::NUMERIC = visible.strike_usd
         AND e.is_put = visible.option_is_put
         AND (visible.lower_expiry IS NULL OR e.expiry >= visible.lower_expiry)
         AND (visible.upper_expiry IS NULL OR e.expiry <= visible.upper_expiry)
        WHERE e.asset = lower(p_asset)
          AND e.chain = p_chain
          AND e.is_settled IS NOT TRUE
          AND e.expiry > p_now
        GROUP BY visible.ordinality
    )
    SELECT
        visible.strike_usd::TEXT,
        visible.option_is_put,
        visible.option_expiry,
        mapped.mapped_count
    FROM mapped
    JOIN visible USING (ordinality)
    ORDER BY visible.ordinality;
END;
$$;

REVOKE ALL ON FUNCTION public.b1n430_touch_order_event_updated_at() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.b1nary_position_page(
    UUID, TEXT, JSONB, TEXT, INTEGER, INTEGER, INTEGER,
    TIMESTAMPTZ, UUID, TIMESTAMPTZ, TIMESTAMPTZ, TEXT
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.b1nary_price_position_counts(
    TEXT, TEXT, BIGINT, JSONB
) FROM PUBLIC, anon, authenticated;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION public.b1nary_position_page(
            UUID, TEXT, JSONB, TEXT, INTEGER, INTEGER, INTEGER,
            TIMESTAMPTZ, UUID, TIMESTAMPTZ, TIMESTAMPTZ, TEXT
        ) TO service_role;
        GRANT EXECUTE ON FUNCTION public.b1nary_price_position_counts(
            TEXT, TEXT, BIGINT, JSONB
        ) TO service_role;
    END IF;
END;
$access$;
