from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202608030005_b1n434_weekly_aggregate.sql"
)


def test_migration_exposes_only_fixed_preflight_and_atomic_write_rpcs():
    lowered = MIGRATION.read_text().lower()
    public_functions = (
        "b1nary_legacy_week_source",
        "b1nary_aggregate_legacy_week",
    )
    for name in public_functions:
        assert f"function public.{name}" in lowered
        assert f"grant execute on function public.{name}" in lowered
        assert f"revoke all on function public.{name}" in lowered
    assert "security definer" not in lowered
    assert lowered.count("security invoker") == 5
    assert lowered.count("set search_path = public, pg_temp") == 2
    assert "from public, anon, authenticated" in lowered
    assert "notify pgrst, 'reload schema'" in lowered


def test_write_is_set_based_locked_atomic_and_idempotent():
    lowered = MIGRATION.read_text().lower()
    assert "pg_advisory_xact_lock" in lowered
    assert "'b1n434:' || week_start_text" in lowered
    assert "p_week_start::text" not in lowered
    assert "with source as materialized" in lowered
    assert "wallet_week as materialized" in lowered
    assert "insert into public.user_weekly_results" in lowered
    assert "insert into public.weekly_reports" in lowered
    assert "on conflict (user_address, week_start) do update" in lowered
    assert "on conflict (week_start) do update" in lowered
    assert lowered.count("is distinct from") == 2
    assert "returning user_address" not in lowered
    assert "returning total_users" not in lowered
    assert "from report_values" in lowered
    assert "previous.week_start < week_start_text" in lowered
    assert "later.week_start > week_start_text" in lowered
    assert "unsafe historical recomputation" in lowered
    assert "select *" not in lowered


def test_migration_preserves_weekly_formulas_and_rounding_stages():
    lowered = MIGRATION.read_text().lower()
    assert "friday 08:00 utc to friday 08:00 utc" in lowered
    assert "p_week_start + interval '7 days'" in lowered
    assert "coalesce(nullif(event.net_premium, 0), event.premium)" in lowered
    assert "event.premium)\n                    ::double precision" in lowered
    assert "source.strike_price::double precision" in lowered
    assert "source.amount::double precision" in lowered
    assert "source.is_put is true" in lowered
    assert "source.is_put is not true" in lowered
    assert "wallet_transitions.phase" in lowered
    assert "order by wallet_transitions.phase" in lowered
    assert "b1n434_ordered_float_sum" in lowered
    assert "wallet_values.rounded_premium::double precision" in lowered
    assert "order by wallet_values.first_indexed_at, wallet_values.wallet" in lowered
    assert "b1n434_python_round" in lowered
    assert "'highest_premium_earned'" in lowered
    assert "'most_active_positions'" in lowered
    assert "'users_with_assignments'" in lowered


def test_migration_reuses_004_generic_index_without_duplicate_or_raw_transport():
    lowered = MIGRATION.read_text().lower()
    assert "idx_order_events_indexed_user_id" in lowered
    assert "create index" not in lowered
    assert ".select(" not in lowered
    assert "representation" not in lowered
    assert "jsonb_agg" not in lowered
