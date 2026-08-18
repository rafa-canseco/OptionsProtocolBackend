from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260721_tokenized_fund_indexer.sql"
).read_text()


def test_ingest_allows_only_exact_terminal_hash_replay() -> None:
    assert "committed_from_block = p_from_block" in MIGRATION
    assert "committed_to_block = p_to_block" in MIGRATION
    assert "committed_block_hash IS DISTINCT FROM p_last_block_hash" in MIGRATION
    assert "RETURN;" in MIGRATION
    assert "Non-contiguous fund window" in MIGRATION


def test_schema_keys_positions_by_adapter_owner() -> None:
    assert "adapter_address TEXT NOT NULL" in MIGRATION
    assert (
        "PRIMARY KEY (chain_id, fund_address, adapter_address, position_id)"
        in MIGRATION
    )


def test_schema_requires_implementations_for_explicit_proxy_roles() -> None:
    assert "'controller', 'batch_settler'" in MIGRATION
    assert "OR implementation_address IS NOT NULL" in MIGRATION


def test_schema_persists_authoritative_accounting_state() -> None:
    assert "active_reporters JSONB NOT NULL" in MIGRATION
    assert "last_report_nonce BIGINT NOT NULL" in MIGRATION
    assert "active_reporters = EXCLUDED.active_reporters" in MIGRATION
    assert "last_report_nonce = EXCLUDED.last_report_nonce" in MIGRATION
