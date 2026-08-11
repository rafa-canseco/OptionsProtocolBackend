from pathlib import Path


MIGRATIONS = Path("supabase/migrations")
MIGRATION_PATH = MIGRATIONS / "202608030000_b1n455_order_events_numeric_premiums.sql"
MIGRATION = MIGRATION_PATH.read_text()


def test_numeric_compatibility_migration_runs_immediately_before_mm_exposure() -> None:
    ordered_migrations = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    migration_index = ordered_migrations.index(MIGRATION_PATH.name)

    assert ordered_migrations[migration_index - 1] == (
        "202608010001_b1n417_wheel_nav_producer.sql"
    )
    assert ordered_migrations[migration_index + 1] == (
        "202608030001_b1n423_mm_exposure.sql"
    )


def test_numeric_compatibility_migration_uses_explicit_numeric_casts() -> None:
    assert "ALTER TABLE public.order_events" in MIGRATION

    for column in ("gross_premium", "net_premium", "protocol_fee"):
        assert (
            f"ALTER COLUMN {column} TYPE NUMERIC\n        USING ({column}::NUMERIC)"
        ) in MIGRATION

    assert "ADD COLUMN" not in MIGRATION
    assert "DROP COLUMN" not in MIGRATION
