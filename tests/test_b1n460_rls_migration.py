import re
from pathlib import Path


MIGRATION_PATH = Path(
    "supabase/migrations/202608150001_b1n460_private_public_tables.sql"
)
MIGRATION = MIGRATION_PATH.read_text()
ASSERTION_MIGRATION = Path(
    "supabase/migrations/202608150002_b1n460_assert_private_public_tables.sql"
).read_text()
ROLE_ASSERTION_MIGRATION = Path(
    "supabase/migrations/202608150003_b1n460_assert_rls_role_attributes.sql"
).read_text()
MIGRATIONS = MIGRATION_PATH.parent


CREATE_PUBLIC_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


def _enables_rls(sql: str, table: str) -> bool:
    return bool(
        re.search(
            rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:public\.)?{re.escape(table)}"
            r"\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
            sql,
            re.IGNORECASE,
        )
    )


def test_hardening_covers_every_current_public_table() -> None:
    normalized = " ".join(MIGRATION.lower().split())

    assert "ns.nspname = 'public'" in normalized
    assert "cls.relkind in ('r', 'p')" in normalized
    assert "alter table %i.%i enable row level security" in normalized
    assert "revoke all privileges on table %i.%i from anon, authenticated" in normalized
    assert (
        "revoke all privileges on all tables in schema public from anon, authenticated"
    ) in normalized
    assert (
        "revoke all privileges on all sequences in schema public "
        "from anon, authenticated"
    ) in normalized


def test_hardening_is_bounded_and_deterministic() -> None:
    normalized = " ".join(MIGRATION.lower().split())

    assert "set lock_timeout = '5s'" in normalized
    assert "order by cls.oid" in normalized
    assert normalized.endswith("reset lock_timeout;")


def test_hardening_keeps_future_public_relations_private_by_default() -> None:
    normalized = " ".join(MIGRATION.lower().split())

    assert (
        normalized.count("alter default privileges for role postgres in schema public")
        == 2
    )
    assert "revoke all privileges on tables from anon, authenticated" in normalized
    assert "revoke all privileges on sequences from anon, authenticated" in normalized


def test_future_public_tables_must_explicitly_enable_rls() -> None:
    future_migrations = sorted(
        path for path in MIGRATIONS.glob("*.sql") if path.name > MIGRATION_PATH.name
    )

    missing_rls = []
    for path in future_migrations:
        sql = path.read_text()
        for table in CREATE_PUBLIC_TABLE.findall(sql):
            if not _enables_rls(sql, table):
                missing_rls.append(f"{path.name}:{table}")

    assert not missing_rls, "public tables missing RLS: " + ", ".join(missing_rls)


def test_hardening_does_not_revoke_backend_service_role() -> None:
    statements = [statement.lower() for statement in MIGRATION.split(";")]
    revoke_statements = [statement for statement in statements if "revoke" in statement]

    assert revoke_statements
    assert all("service_role" not in statement for statement in revoke_statements)


def test_rollout_asserts_effective_rls_and_role_privileges() -> None:
    normalized = " ".join(ASSERTION_MIGRATION.lower().split())

    assert "and not cls.relrowsecurity" in normalized
    for role in ("anon", "authenticated"):
        for privilege in ("select", "insert", "update", "delete"):
            assert (
                f"has_table_privilege('{role}', cls.oid, '{privilege}')" in normalized
            )
    for privilege in ("select", "insert", "update", "delete"):
        assert (
            f"not has_table_privilege('service_role', cls.oid, '{privilege}')"
            in normalized
        )


def test_rollout_asserts_rls_bypass_role_attributes() -> None:
    normalized = " ".join(ROLE_ASSERTION_MIGRATION.lower().split())

    for role in ("anon", "authenticated", "service_role"):
        assert (
            f"select rolbypassrls from pg_roles where rolname = '{role}'" in normalized
        )
    assert "service_role cannot bypass rls" in normalized
