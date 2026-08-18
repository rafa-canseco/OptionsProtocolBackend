from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/202607280001_b1n361_versioned_proxy_reapply.sql"
).read_text()


def test_versioned_reapply_accepts_historical_proxy_bindings() -> None:
    assert "array_agg(DISTINCT role ORDER BY role)" in MIGRATION
    assert "one active binding per role" in MIGRATION
    assert "DELETE FROM v2_fund_contracts" in MIGRATION


def test_same_address_reapply_preserves_indexed_fund_state() -> None:
    assert "SELECT fund_key" in MIGRATION
    assert "IF FOUND THEN" in MIGRATION
    assert "DELETE FROM v2_fund_registry" not in MIGRATION
    assert "UPDATE v2_fund_registry" in MIGRATION
