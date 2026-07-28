from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/202607280002_b1n361_monotonic_confirmed_head.sql"
).read_text()


def test_confirmed_head_upsert_is_atomic_and_monotonic() -> None:
    assert "CREATE FUNCTION v2_upsert_confirmed_chain_head" in MIGRATION
    assert "ON CONFLICT (chain_id) DO UPDATE" in MIGRATION
    assert (
        "WHERE v2_confirmed_chain_heads.block_number <= EXCLUDED.block_number"
    ) in MIGRATION


def test_confirmed_head_upsert_is_restricted_to_service_role() -> None:
    assert "REVOKE EXECUTE ON FUNCTION v2_upsert_confirmed_chain_head" in MIGRATION
    assert ") FROM PUBLIC;" in MIGRATION
    assert ") TO service_role;" in MIGRATION
