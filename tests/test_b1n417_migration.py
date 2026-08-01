from pathlib import Path


MIGRATION = Path("supabase/migrations/202607310001_b1n417_meta_wheel_read_model.sql")


def test_meta_wheel_migration_is_isolated_and_reorg_safe() -> None:
    sql = MIGRATION.read_text()

    assert "'csp', 'covered_call', 'meta_wheel'" in sql
    assert "CREATE TABLE v2_meta_wheel_tranches" in sql
    assert "CREATE TABLE v2_meta_wheel_assignment_lots" in sql
    assert "CREATE TABLE v2_meta_wheel_handoffs" in sql
    assert "CREATE TABLE v2_meta_wheel_lane_valuations" in sql
    assert "CREATE TABLE v2_meta_wheel_nav_snapshots" in sql
    assert "v2_ingest_fund_window_b1n417" in sql
    assert "Meta Wheel NAV nonce is already bound to another block" in sql
    assert "strategy IS DISTINCT FROM 'meta_wheel'" in sql
    assert "v2_fund_strategy_positions" not in sql
    assert "wheel_coordinator" in sql
    assert "meta_wheel_valuator" in sql
    assert "reserved_principal_usdc NUMERIC(78, 0)" in sql
    assert "child_execution_state_hash TEXT" in sql
    assert "'pending_delivery', 'csp_otm', 'csp_assigned'" in sql
    assert "child_position_hash TEXT" not in sql
    assert "lane_id" not in sql


def test_meta_wheel_registry_binds_only_its_canonical_accounting_role() -> None:
    sql = MIGRATION.read_text()

    assert "ADD COLUMN IF NOT EXISTS accounting_role_account TEXT" in sql
    assert "strategy_kind = 'meta_wheel'" in sql
    assert "AND accounting_role_account IS NOT NULL" in sql
    assert "accounting_role_account ~ '^0x[0-9a-f]{40}$'" in sql
    assert "strategy_kind IN ('csp', 'covered_call')" in sql
    assert "AND accounting_role_account IS NULL" in sql
    assert "Meta Wheel handoff requires its canonical accounting role" in sql
    assert "Standalone handoff cannot bind a Meta Wheel accounting role" in sql
