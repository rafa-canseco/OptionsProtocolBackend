from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/202607280003_b1n388_incremental_fund_history.sql"
).read_text()


def test_migration_indexes_compacted_replay_paths() -> None:
    assert "v2_chain_events_state_replay_idx" in MIGRATION
    assert "v2_chain_events_latest_nav_idx" in MIGRATION
    assert "event_name NOT IN ('NavCommitted', 'NavSubmitted')" in MIGRATION


def test_migration_keeps_nav_and_activity_history_incremental() -> None:
    function = MIGRATION.split(
        "CREATE OR REPLACE FUNCTION v2_ingest_fund_window_b1n340", 1
    )[1]
    assert "DELETE FROM v2_nav_reports" not in function
    assert "DELETE FROM v2_fund_activity" not in function
    assert "ON CONFLICT (chain_id, fund_address, report_nonce) DO UPDATE" in function
    assert (
        "chain_id, fund_address, transaction_hash, log_index\n"
        "        ) DO UPDATE" in function
    )
