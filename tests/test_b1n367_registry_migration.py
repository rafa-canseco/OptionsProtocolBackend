from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "202607260002_b1n367_rotate_fund_contract_binding.sql"
).read_text()


def test_contract_rotation_is_atomic_and_expected_address_bound() -> None:
    assert "LOCK TABLE v2_fund_contracts IN SHARE ROW EXCLUSIVE MODE" in MIGRATION
    assert "FOR UPDATE" in MIGRATION
    assert "Active contract binding differs from expected address" in MIGRATION
    assert "SET valid_to_block = p_activation_block - 1" in MIGRATION
    assert "p_activation_block," in MIGRATION


def test_contract_rotation_is_service_role_only() -> None:
    signature = (
        "v2_rotate_fund_contract_binding(\n"
        "    BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, TEXT, BIGINT\n"
        ")"
    )
    assert f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, anon, authenticated" in MIGRATION
    assert f"GRANT EXECUTE ON FUNCTION {signature} TO service_role" in MIGRATION
