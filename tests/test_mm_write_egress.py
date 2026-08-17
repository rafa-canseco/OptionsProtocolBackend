import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from postgrest.types import CountMethod, ReturnMethod

from src.api.mm_routes import _prune_stale_quotes_for_mm, cancel_quotes, submit_quotes
from src.models.mm import QuoteBatchRequest, QuoteSubmission


MM_ADDRESS = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _base_quote() -> QuoteSubmission:
    return QuoteSubmission(
        otoken_address="0x" + ("b" * 40),
        bid_price=1_000_000,
        deadline=2_000,
        quote_id=7,
        max_amount=100_000_000,
        maker_nonce=3,
        signature="0x" + ("c" * 130),
        chain="base",
        asset="eth",
        strike_price=2_500,
        expiry=3_000,
        is_put=True,
    )


def test_submit_quotes_discards_write_representations() -> None:
    db = MagicMock()
    body = QuoteBatchRequest(quotes=[_base_quote()])

    with (
        patch("src.api.mm_routes.time.time", return_value=1_000),
        patch("src.api.mm_routes._resolve_nonce", return_value=(MM_ADDRESS, 3)),
        patch("src.api.mm_routes._verify_base_sig", return_value=True),
        patch("src.api.mm_routes.get_client", return_value=db),
        patch("src.api.mm_routes._prune_stale_quotes_for_mm"),
    ):
        result = asyncio.run(submit_quotes(body=body, mm_address=MM_ADDRESS))

    assert result.accepted == 1
    db.table.return_value.update.assert_called_once_with(
        {"is_active": False}, returning=ReturnMethod.minimal
    )
    assert db.table.return_value.upsert.call_args.kwargs == {
        "on_conflict": "mm_address,quote_id",
        "returning": ReturnMethod.minimal,
    }


def test_existing_stale_quote_prune_uses_minimal_return() -> None:
    db = MagicMock()

    _prune_stale_quotes_for_mm(db, MM_ADDRESS, "base", 1_000)

    db.table.return_value.delete.assert_called_once_with(returning=ReturnMethod.minimal)


def test_cancel_quotes_preserves_count_without_returning_rows() -> None:
    db = MagicMock()
    execute = db.table.return_value.update.return_value.eq.return_value.eq.return_value.execute
    execute.return_value = SimpleNamespace(data=[], count=4)

    with patch("src.api.mm_routes.get_client", return_value=db):
        result = asyncio.run(cancel_quotes(mm_address=MM_ADDRESS))

    assert result == {"cancelled": 4}
    db.table.return_value.update.assert_called_once_with(
        {"is_active": False},
        count=CountMethod.exact,
        returning=ReturnMethod.minimal,
    )
