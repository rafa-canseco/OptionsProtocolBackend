-- B1N-361: independent fund-indexer replicas must never move the shared
-- confirmed chain head backwards during a rolling deployment.

CREATE FUNCTION v2_upsert_confirmed_chain_head(
    p_chain_id BIGINT,
    p_block_number BIGINT,
    p_block_hash TEXT,
    p_observed_at TIMESTAMPTZ
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_chain_id <= 0
       OR p_block_number < 0
       OR p_block_hash IS NULL
       OR p_block_hash = ''
       OR p_observed_at IS NULL THEN
        RAISE EXCEPTION 'Invalid confirmed chain head';
    END IF;

    INSERT INTO v2_confirmed_chain_heads (
        chain_id,
        block_number,
        block_hash,
        observed_at
    ) VALUES (
        p_chain_id,
        p_block_number,
        p_block_hash,
        p_observed_at
    )
    ON CONFLICT (chain_id) DO UPDATE SET
        block_number = EXCLUDED.block_number,
        block_hash = EXCLUDED.block_hash,
        observed_at = EXCLUDED.observed_at
    WHERE v2_confirmed_chain_heads.block_number <= EXCLUDED.block_number;

    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

REVOKE EXECUTE ON FUNCTION v2_upsert_confirmed_chain_head(
    BIGINT, BIGINT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION v2_upsert_confirmed_chain_head(
            BIGINT, BIGINT, TEXT, TIMESTAMPTZ
        ) TO service_role;
    END IF;
END;
$access$;
