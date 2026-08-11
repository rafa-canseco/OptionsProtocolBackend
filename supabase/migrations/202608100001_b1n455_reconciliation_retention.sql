-- B1N-455: bound per-fund reconciliation telemetry while preserving canonical ingest.

CREATE INDEX IF NOT EXISTS v2_fund_reconciliations_retention_idx
    ON v2_fund_reconciliations (
        chain_id, fund_address, block_number DESC
    );

ALTER FUNCTION v2_ingest_fund_window(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) RENAME TO v2_ingest_fund_window_b1n455;

CREATE OR REPLACE FUNCTION v2_ingest_fund_window(
    p_chain_id BIGINT,
    p_fund_address TEXT,
    p_indexer_name TEXT,
    p_from_block BIGINT,
    p_to_block BIGINT,
    p_last_block_hash TEXT,
    p_events JSONB,
    p_projection JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    PERFORM v2_ingest_fund_window_b1n455(
        p_chain_id, p_fund_address, p_indexer_name, p_from_block,
        p_to_block, p_last_block_hash, p_events, p_projection
    );

    DELETE FROM v2_fund_reconciliations AS reconciliations
    WHERE reconciliations.chain_id = p_chain_id
      AND reconciliations.fund_address = p_fund_address
      AND reconciliations.block_number < (
          SELECT retained.block_number
          FROM v2_fund_reconciliations AS retained
          WHERE retained.chain_id = p_chain_id
            AND retained.fund_address = p_fund_address
          ORDER BY retained.block_number DESC
          OFFSET 2047
          LIMIT 1
      );
END;
$$;

REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n455(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window(
    BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n340(
            BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
        ) FROM service_role;
        REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n353(
            BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
        ) FROM service_role;
        REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n417(
            BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
        ) FROM service_role;
        REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n455(
            BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
        ) FROM service_role;
        GRANT EXECUTE ON FUNCTION v2_ingest_fund_window(
            BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB
        ) TO service_role;
    END IF;
END;
$access$;

NOTIFY pgrst, 'reload schema';
