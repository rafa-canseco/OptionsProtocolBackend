from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202607280003_b1n389_lazy_otoken_series.sql"
).read_text()


def test_lazy_series_migration_has_lifecycle_and_canonical_constraints() -> None:
    assert (
        "deployment_status IN ('virtual', 'creating', 'ready', 'failed')" in MIGRATION
    )
    assert "available_otokens_chain_series_key_idx" in MIGRATION
    assert "WHERE series_key IS NOT NULL" in MIGRATION
    assert "strike_price_raw NUMERIC(78, 0)" in MIGRATION
    assert "chain_id BIGINT" in MIGRATION


def test_materialization_claim_is_cross_process_and_bounded() -> None:
    assert "CREATE OR REPLACE FUNCTION v1_claim_otoken_materialization" in MIGRATION
    assert "FOR UPDATE" in MIGRATION
    assert "deployment_owner_token" in MIGRATION
    assert "deployment_lease_expires_at" in MIGRATION
    assert "creation_attempts >= p_max_attempts" in MIGRATION
    assert "wallet_address = lower(p_wallet_address)" in MIGRATION
    assert "WHERE series_key = p_series_key" in MIGRATION
    assert MIGRATION.count("pg_advisory_xact_lock") == 3
    assert "otoken-actor:" in MIGRATION
    assert "otoken-wallet:" in MIGRATION
    assert "otoken-series:" in MIGRATION
    assert "otoken_materialization_wallet_created_idx" in MIGRATION
    assert MIGRATION.index("FOR UPDATE") < MIGRATION.index(
        "SELECT outcome INTO existing_outcome"
    )
    assert MIGRATION.count("outcome <> 'rate_limited'") == 5
    assert "existing_outcome = 'rate_limited'" in MIGRATION
    assert "THEN 'requested'" in MIGRATION


def test_materialization_functions_are_service_role_only() -> None:
    assert "FROM PUBLIC, anon, authenticated" in MIGRATION
    assert "TO service_role" in MIGRATION
    assert "otoken_materialization_intents ENABLE ROW LEVEL SECURITY" in MIGRATION
