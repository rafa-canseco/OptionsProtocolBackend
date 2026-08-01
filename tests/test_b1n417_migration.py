from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/202607310001_b1n417_meta_wheel_read_model.sql"
)


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
