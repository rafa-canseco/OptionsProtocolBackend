from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/"
    "202607270001_b1n366_recover_replaced_nav_transactions.sql"
).read_text()


def test_recovery_migration_clears_terminal_transaction_material() -> None:
    assert "CREATE FUNCTION v2_release_failed_nav_report_transaction" in MIGRATION
    assert "'TRANSACTION_NONCE_CONSUMED'" in MIGRATION
    assert "failed_transaction_hash = transaction_hash" in MIGRATION
    assert "transaction_hash = NULL" in MIGRATION
    assert "signed_transaction = NULL" in MIGRATION
    assert "ownership_token = NULL" in MIGRATION


def test_recovery_migration_is_compare_and_swap_guarded() -> None:
    assert "AND id = p_run_id" in MIGRATION
    assert "AND transaction_hash = p_transaction_hash" in MIGRATION
    assert "AND status IN ('reconciling', 'submitted')" in MIGRATION
