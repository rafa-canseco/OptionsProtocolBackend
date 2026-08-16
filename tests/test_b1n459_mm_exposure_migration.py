from pathlib import Path
import re


MIGRATION = Path("supabase/migrations/202608030001_b1n423_mm_exposure.sql").read_text()


def test_exposure_rpc_returns_one_aggregate_row() -> None:
    assert "CREATE OR REPLACE FUNCTION public.v1_get_mm_exposure" in MIGRATION
    assert "RETURNS TABLE" in MIGRATION
    assert "JSONB_AGG" in MIGRATION
    assert "fill_by_expiry AS MATERIALIZED" in MIGRATION
    assert "GROUP BY e.expiry" in MIGRATION
    assert "COUNT(*) FILTER" in MIGRATION
    assert "SELECT * FROM public.order_events" not in MIGRATION
    assert "active_quotes_notional TEXT" in MIGRATION
    assert "total_premium_earned TEXT" in MIGRATION
    assert "'total_amount', o.total_amount::TEXT" in MIGRATION


def test_exposure_rpc_preserves_legacy_classification_semantics() -> None:
    assert "f.expiry_value > p_now_ts" in MIGRATION
    assert "e.expiry <= p_now_ts" in MIGRATION
    assert "e.expiry <> 0" in MIGRATION
    assert "e.is_settled IS NOT TRUE" in MIGRATION
    assert "COALESCE(NULLIF(e.gross_premium, 0), e.premium, 0)" in MIGRATION
    assert "q.is_active = TRUE" in MIGRATION
    assert "q.deadline > p_now_ts" in MIGRATION


def test_exposure_rpc_caps_ordered_future_expiry_buckets_only() -> None:
    open_positions = re.search(
        r"open_by_expiry AS \((.*?)\),\nexpiry_totals AS",
        MIGRATION,
        flags=re.DOTALL,
    )
    assert open_positions is not None
    assert "WHERE f.expiry_value > p_now_ts" in open_positions.group(1)
    assert "ORDER BY f.expiry_value" in open_positions.group(1)
    assert "LIMIT 100" in open_positions.group(1)

    all_history_totals = re.search(
        r"fill_totals AS \((.*?)\),\nopen_by_expiry AS",
        MIGRATION,
        flags=re.DOTALL,
    )
    assert all_history_totals is not None
    assert "FROM fill_by_expiry AS f" in all_history_totals.group(1)
    assert "LIMIT" not in all_history_totals.group(1)


def test_exposure_rpc_is_idempotent_service_role_only_and_invoker_safe() -> None:
    signature = "public.v1_get_mm_exposure(TEXT, BIGINT)"
    assert f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC" in MIGRATION
    assert f"GRANT EXECUTE ON FUNCTION {signature}" in MIGRATION
    assert "TO service_role" in MIGRATION
    assert "SECURITY INVOKER" in MIGRATION
    assert "SET search_path = public" in MIGRATION
    assert "NOTIFY pgrst, 'reload schema'" in MIGRATION


def test_exposure_migration_has_no_data_mutation_or_destructive_ddl() -> None:
    forbidden = (
        r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|DROP\s+TABLE|ALTER\s+TABLE|BACKFILL)\b"
    )
    assert re.search(forbidden, MIGRATION, flags=re.IGNORECASE) is None
