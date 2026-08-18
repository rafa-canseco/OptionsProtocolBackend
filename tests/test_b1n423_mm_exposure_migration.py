from pathlib import Path


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


def test_exposure_rpc_is_service_role_only_and_refreshes_postgrest() -> None:
    signature = "public.v1_get_mm_exposure(TEXT, BIGINT)"
    assert f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC" in MIGRATION
    assert f"GRANT EXECUTE ON FUNCTION {signature}" in MIGRATION
    assert "TO service_role" in MIGRATION
    assert "NOTIFY pgrst, 'reload schema'" in MIGRATION
