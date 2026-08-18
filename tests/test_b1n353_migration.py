from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260722_b1n353_fund_product_nav.sql"
).read_text()
FAIR_VALUE_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202607260001_b1n366_csp_fair_value_marks.sql"
).read_text()


def test_registry_metadata_is_required_for_enabled_deployments() -> None:
    assert "v2_fund_registry_enabled_metadata" in MIGRATION
    assert "accounting_asset_symbol IS NOT NULL" in MIGRATION
    assert "share_decimals IS NOT NULL" in MIGRATION
    assert "INSERT INTO v2_fund_registry" not in MIGRATION
    assert "UPDATE v2_fund_registry SET enabled = FALSE WHERE enabled" in MIGRATION


def test_coherent_state_payload_columns_match_python() -> None:
    fields = {
        "accounted_idle_assets",
        "virtual_shares",
        "deposits_paused",
        "redemptions_paused",
        "execution_lock_owner",
        "has_active_processing",
        "fund_flow_nonce",
        "idle_state_hash",
        "as_of_block",
        "as_of_block_hash",
        "reconciled",
        "indexed_at",
    }
    assert all(f"state->>'{field}'" in MIGRATION for field in fields)
    assert "CREATE OR REPLACE FUNCTION v2_ingest_fund_window" in MIGRATION
    assert "v2_ingest_fund_window_b1n340" in MIGRATION


def test_replayed_window_returns_before_new_projection_writes() -> None:
    replay_guard = MIGRATION.index("IF expected_block = p_to_block + 1")
    replay_return = MIGRATION.index("RETURN;", replay_guard)
    predecessor_call = MIGRATION.index("PERFORM v2_ingest_fund_window_b1n340")
    state_update = MIGRATION.index("UPDATE v2_fund_state SET", predecessor_call)

    assert replay_guard < replay_return < predecessor_call < state_update


def test_report_runs_are_idempotent_and_record_blocked_status() -> None:
    assert "CREATE TABLE v2_nav_report_runs" in MIGRATION
    assert "'building', 'blocked', 'simulated', 'reconciling'," in MIGRATION
    assert "v2_nav_report_runs_attempt_idx" in MIGRATION
    assert "ON v2_nav_report_runs (chain_id, fund_address, report_nonce)" in MIGRATION
    assert "snapshot_block, report_nonce" not in MIGRATION


def test_report_run_claim_and_updates_are_atomic_and_leased() -> None:
    assert "ownership_token UUID" in MIGRATION
    assert "lease_expires_at TIMESTAMPTZ" in MIGRATION
    assert "CREATE FUNCTION v2_claim_nav_report_run" in MIGRATION
    assert "CREATE FUNCTION v2_update_nav_report_run" in MIGRATION
    assert "FOR UPDATE" in MIGRATION
    assert "run.status IN ('blocked', 'failed')" in MIGRATION
    assert "run.status IN ('building', 'simulated')" in MIGRATION
    assert "transaction_hash IS NULL" in MIGRATION
    assert "ownership_token = p_ownership_token" in MIGRATION


def test_signed_transaction_material_is_service_only_and_paired() -> None:
    assert "ENABLE ROW LEVEL SECURITY" in MIGRATION
    assert "(transaction_hash IS NULL) = (signed_transaction IS NULL)" in MIGRATION
    assert ") FROM PUBLIC" in MIGRATION
    assert ") TO service_role" in MIGRATION


def test_operational_tables_and_legacy_ingest_are_service_only() -> None:
    assert "v2_confirmed_chain_heads ENABLE ROW LEVEL SECURITY" in MIGRATION
    assert "v2_csp_option_observations ENABLE ROW LEVEL SECURITY" in MIGRATION
    assert "v2_redemption_batch_states ENABLE ROW LEVEL SECURITY" in MIGRATION
    assert "REVOKE EXECUTE ON FUNCTION v2_ingest_fund_window_b1n340" in MIGRATION
    assert "SECURITY DEFINER" in MIGRATION


def test_confirmed_revert_finalization_is_guarded_and_forensic() -> None:
    assert "failed_transaction_hash TEXT" in MIGRATION
    assert "failed_transaction_error TEXT" in MIGRATION
    assert "failed_transaction_at TIMESTAMPTZ" in MIGRATION
    assert "CREATE FUNCTION v2_record_reverted_nav_report_run" in MIGRATION
    assert "AND id = p_run_id" in MIGRATION
    assert "AND transaction_hash = p_transaction_hash" in MIGRATION
    assert "AND status IN ('reconciling', 'submitted')" in MIGRATION
    assert "signed_transaction = NULL" in MIGRATION
    assert "status = 'failed'" in MIGRATION


def test_verified_observations_and_confirmed_heads_are_persisted() -> None:
    assert "CREATE TABLE v2_csp_option_observations" in MIGRATION
    assert "snapshot_block, observer_address" in MIGRATION
    assert "UNIQUE (chain_id, valuator_address, digest)" in MIGRATION
    assert "CREATE TABLE v2_confirmed_chain_heads" in MIGRATION


def test_fair_value_metadata_is_exact_block_and_service_only() -> None:
    assert "CREATE TABLE v2_csp_fair_value_marks" in FAIR_VALUE_MIGRATION
    assert "snapshot_block_hash TEXT NOT NULL" in FAIR_VALUE_MIGRATION
    assert "fair_liability_assets NUMERIC" in FAIR_VALUE_MIGRATION
    assert "stress_liability_assets NUMERIC" in FAIR_VALUE_MIGRATION
    assert "source_quality TEXT NOT NULL" in FAIR_VALUE_MIGRATION
    assert "ENABLE ROW LEVEL SECURITY" in FAIR_VALUE_MIGRATION
    assert "GRANT ALL ON v2_csp_fair_value_marks TO service_role" in (
        FAIR_VALUE_MIGRATION
    )


def test_latest_redemption_batch_state_is_persisted_per_controller() -> None:
    assert "CREATE TABLE v2_redemption_batch_states" in MIGRATION
    assert "latest_batch_id BIGINT" in MIGRATION
    assert "processing BOOLEAN NOT NULL" in MIGRATION
    assert "unwind_committed BOOLEAN NOT NULL" in MIGRATION
    assert "p_projection->'redemption_batch_states'" in MIGRATION
