from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260723_b1n353_deployment_handoff.sql"
).read_text()


def test_deployment_replacement_is_atomic_and_service_only() -> None:
    assert "CREATE OR REPLACE FUNCTION v2_replace_fund_deployment" in MIGRATION
    assert "LOCK TABLE v2_fund_registry" in MIGRATION
    assert "v2_fund_registry_enabled_fund_key_idx" in MIGRATION
    assert "WHERE enabled" in MIGRATION
    assert "deployment_status = 'RETIRED'" in MIGRATION
    assert "enabled = TRUE" in MIGRATION
    assert "p_registry->>'handoff_ready'" in MIGRATION
    assert "fully configured and reconciled" in MIGRATION
    assert "REVOKE ALL ON FUNCTION" in MIGRATION
    assert "TO service_role" in MIGRATION


def test_existing_target_cannot_erase_binding_history() -> None:
    assert "DROP CONSTRAINT IF EXISTS v2_fund_registry_fund_key_key" in MIGRATION
    assert "Target fund is already registered" in MIGRATION
    assert "use versioned bindings for upgrades" in MIGRATION
