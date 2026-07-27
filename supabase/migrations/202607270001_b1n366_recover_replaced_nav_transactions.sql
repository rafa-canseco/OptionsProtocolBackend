-- B1N-366: release dropped or replaced NAV transactions so the same report
-- nonce can be rebuilt with a fresh snapshot, validity window and account nonce.

CREATE FUNCTION v2_release_failed_nav_report_transaction(
    p_chain_id BIGINT,
    p_fund_address TEXT,
    p_report_nonce BIGINT,
    p_run_id UUID,
    p_transaction_hash TEXT,
    p_reason_code TEXT,
    p_error TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_reason_code NOT IN (
        'TRANSACTION_NONCE_CONSUMED',
        'TRANSACTION_REPLACED',
        'TRANSACTION_DROPPED'
    ) THEN
        RAISE EXCEPTION 'Invalid terminal NAV transaction reason';
    END IF;

    UPDATE v2_nav_report_runs SET
        status = 'failed',
        reason_code = p_reason_code,
        failed_transaction_hash = transaction_hash,
        failed_transaction_error = coalesce(p_error, p_reason_code),
        failed_transaction_at = now(),
        transaction_hash = NULL,
        signed_transaction = NULL,
        ownership_token = NULL,
        lease_expires_at = NULL,
        updated_at = now()
    WHERE chain_id = p_chain_id
      AND fund_address = p_fund_address
      AND report_nonce = p_report_nonce
      AND id = p_run_id
      AND status IN ('reconciling', 'submitted')
      AND transaction_hash = p_transaction_hash
      AND signed_transaction IS NOT NULL;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

REVOKE EXECUTE ON FUNCTION v2_release_failed_nav_report_transaction(
    BIGINT, TEXT, BIGINT, UUID, TEXT, TEXT, TEXT
) FROM PUBLIC;

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION v2_release_failed_nav_report_transaction(
            BIGINT, TEXT, BIGINT, UUID, TEXT, TEXT, TEXT
        ) TO service_role;
    END IF;
END;
$access$;
