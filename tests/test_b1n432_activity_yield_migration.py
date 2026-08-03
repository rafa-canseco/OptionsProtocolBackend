from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202608030003_b1n432_activity_yield_reads.sql"
)


def test_migration_has_exactly_five_bounded_service_role_rpcs():
    sql = MIGRATION.read_text()
    lowered = sql.lower()
    names = (
        "b1nary_activity_summary",
        "b1nary_yield_user_summary",
        "b1nary_yield_position_page",
        "b1nary_yield_history_page",
        "b1nary_yield_stats",
    )

    for name in names:
        assert f"function public.{name}" in lowered
        assert f"grant execute on function public.{name}" in lowered
        assert f"revoke all on function public.{name}" in lowered

    assert lowered.count("security invoker") == 5
    assert lowered.count("set search_path = public, pg_temp") == 5
    assert lowered.count("to service_role") == 5
    assert "from public, anon, authenticated" in lowered
    assert "notify pgrst, 'reload schema'" in lowered


def test_migration_enforces_projection_caps_and_keysets():
    lowered = MIGRATION.read_text().lower()

    assert "limit p_limit + 1" in lowered
    assert lowered.count("limit p_limit + 1") == 2
    assert lowered.count("p_limit is null or p_limit < 1 or p_limit > 100") == 2
    assert (
        "(position.deposited_at, position.id) < (p_cursor_at, p_cursor_id)" in lowered
    )
    assert (
        "(allocation.created_at, allocation.id) < (p_cursor_at, p_cursor_id)" in lowered
    )
    assert "position.created_at <= effective_watermark" in lowered
    assert "allocation.created_at <= effective_watermark" in lowered
    assert "select *" not in lowered


def test_migration_has_additive_indexes_for_pages_and_weights():
    lowered = MIGRATION.read_text().lower()

    assert "idx_b1n432_yield_allocations_history" in lowered
    assert "(user_address, created_at desc, id desc)" in lowered
    assert "idx_b1n432_yield_positions_user_page" in lowered
    assert "(user_address, deposited_at desc, id desc)" in lowered
    assert "idx_b1n432_yield_positions_overlap_active" in lowered
    assert "(deposited_at, asset)" in lowered
    assert "where settled_at is null" in lowered
    assert "idx_b1n432_yield_positions_overlap_settled" in lowered
    assert "(settled_at, deposited_at, asset)" in lowered
    assert "where settled_at is not null" in lowered
    assert "idx_b1n432_yield_distributions_period_end" in lowered
    assert "(period_end desc)" in lowered
    assert "drop table" not in lowered
    assert "delete from" not in lowered
    assert "truncate" not in lowered


def test_migration_preserves_activity_and_global_weight_semantics():
    lowered = MIGRATION.read_text().lower()

    assert "coalesce(nullif(event.net_premium, 0), event.premium, 0)" in lowered
    assert "when event.is_put is not false" in lowered
    assert "lower(coalesce(event.asset, 'eth')) = 'btc'" in lowered
    assert "event.indexed_at at time zone 'utc'" in lowered
    assert "greatest(\n                ((current_timestamp" not in lowered
    assert "coalesce(event.collateral_usd, 0)::double precision" in lowered
    assert "round(total_volume, 2)" not in lowered
    assert "estimated_accruing_raw" in lowered
    assert "sum(trunc(" in lowered
    assert "'totals', result_totals" in lowered
    assert "as materialized" not in lowered
    assert "as not materialized" in lowered
    assert "global_weight" in lowered
    assert "user_weight" in lowered
    assert "'2026-04-02t00:00:00z'::timestamptz" in lowered
    assert "p_period_start timestamptz" in lowered
    assert "period_start := p_period_start" in lowered
    assert "yield position continuation requires period_start" in lowered
    assert "first yield position page must resolve period_start" in lowered
