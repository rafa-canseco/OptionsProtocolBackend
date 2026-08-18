from pathlib import Path


MIGRATION_PATH = Path(
    "supabase/migrations/202608100001_b1n455_reconciliation_retention.sql"
)
MIGRATION = MIGRATION_PATH.read_text()
SIGNATURE = "BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, JSONB, JSONB"


def test_retention_migration_wraps_the_canonical_ingest_first() -> None:
    assert MIGRATION_PATH.name > "202608030005_b1n434_weekly_aggregate.sql"
    assert (
        "ALTER FUNCTION v2_ingest_fund_window(\n"
        f"    {SIGNATURE}\n"
        ") RENAME TO v2_ingest_fund_window_b1n455;"
    ) in MIGRATION
    delegate = MIGRATION.index("PERFORM v2_ingest_fund_window_b1n455(")
    prune = MIGRATION.index("DELETE FROM v2_fund_reconciliations")
    assert delegate < prune
    assert "SECURITY DEFINER" in MIGRATION
    assert "SET search_path = public" in MIGRATION


def test_retention_keeps_the_newest_2048_rows_for_only_the_target_fund() -> None:
    assert "reconciliations.chain_id = p_chain_id" in MIGRATION
    assert "reconciliations.fund_address = p_fund_address" in MIGRATION
    assert "retained.chain_id = p_chain_id" in MIGRATION
    assert "retained.fund_address = p_fund_address" in MIGRATION
    assert "reconciliations.block_number < (" in MIGRATION
    assert "ORDER BY retained.block_number DESC" in MIGRATION
    assert "OFFSET 2047" in MIGRATION
    assert "LIMIT 1" in MIGRATION


def test_retention_has_a_supporting_per_fund_index() -> None:
    assert (
        "CREATE INDEX IF NOT EXISTS v2_fund_reconciliations_retention_idx" in MIGRATION
    )
    assert ("chain_id, fund_address, block_number DESC\n    );") in MIGRATION


def test_wrapper_preserves_execute_posture_and_refreshes_postgrest() -> None:
    prior_signature = f"v2_ingest_fund_window_b1n455(\n    {SIGNATURE}\n)"
    current_signature = f"v2_ingest_fund_window(\n    {SIGNATURE}\n)"
    assert f"REVOKE EXECUTE ON FUNCTION {prior_signature} FROM PUBLIC" in MIGRATION
    assert f"REVOKE EXECUTE ON FUNCTION {current_signature} FROM PUBLIC" in MIGRATION
    assert "NOTIFY pgrst, 'reload schema'" in MIGRATION
    assert "v1_get_mm_exposure" not in MIGRATION


def test_service_role_can_execute_only_the_retaining_canonical_wrapper() -> None:
    access_block = MIGRATION.split("DO $access$", maxsplit=1)[1].split(
        "$access$;", maxsplit=1
    )[0]
    assert "rolname = 'service_role'" in access_block

    for alias in ("b1n340", "b1n353", "b1n417", "b1n455"):
        internal_signature = (
            f"v2_ingest_fund_window_{alias}(\n            {SIGNATURE}\n        )"
        )
        assert (
            f"REVOKE EXECUTE ON FUNCTION {internal_signature} FROM service_role;"
            in access_block
        )

    canonical_grant = (
        "GRANT EXECUTE ON FUNCTION v2_ingest_fund_window(\n"
        f"            {SIGNATURE}\n"
        "        ) TO service_role;"
    )
    assert canonical_grant in access_block
    assert access_block.count("GRANT EXECUTE ON FUNCTION") == 1
