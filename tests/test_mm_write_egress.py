import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from postgrest.types import CountMethod, ReturnMethod
from pydantic import ValidationError

from src.api.mm_routes import (
    _prune_stale_quotes_for_mm,
    cancel_quotes,
    report_capacity,
    submit_quotes,
)
from src.models.mm import CapacityUpdateRequest, QuoteBatchRequest, QuoteSubmission


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


@pytest.mark.parametrize("asset", ("nvdac", "cbzec", "cbhype", "vvv"))
def test_routed_assets_are_valid_quote_and_capacity_models(asset: str) -> None:
    quote = QuoteSubmission(**{**_base_quote().model_dump(), "asset": asset})
    capacity = CapacityUpdateRequest(
        asset=asset,
        capacity_eth=1,
        capacity_usd=100,
        status="active",
    )
    assert quote.asset == asset
    assert capacity.asset == asset


def test_unknown_assets_remain_rejected() -> None:
    with pytest.raises(ValidationError, match="asset must be one of"):
        QuoteSubmission(**{**_base_quote().model_dump(), "asset": "unknown"})
    with pytest.raises(ValidationError, match="asset must be one of"):
        CapacityUpdateRequest(
            asset="unknown",
            capacity_eth=1,
            capacity_usd=100,
            status="active",
        )


def test_routed_quote_publish_fails_closed_when_publishing_disabled() -> None:
    quote = QuoteSubmission(**{**_base_quote().model_dump(), "asset": "nvdac"})
    body = QuoteBatchRequest(quotes=[quote])
    with (
        patch("src.api.mm_routes.settings.routed_settlement_publishing_enabled", False),
        patch("src.api.mm_routes.get_asset_price") as price,
        patch("src.api.mm_routes._resolve_nonce") as nonce,
        patch("src.api.mm_routes.get_client") as db,
        pytest.raises(HTTPException) as exc,
    ):
        asyncio.run(submit_quotes(body=body, mm_address=MM_ADDRESS))
    assert exc.value.status_code == 503
    price.assert_not_called()
    nonce.assert_not_called()
    db.assert_not_called()


def test_routed_quote_publish_fails_closed_on_market_preflight() -> None:
    quote = QuoteSubmission(**{**_base_quote().model_dump(), "asset": "nvdac"})
    body = QuoteBatchRequest(quotes=[quote])
    with (
        patch("src.api.mm_routes.settings.routed_settlement_publishing_enabled", True),
        patch("src.api.mm_routes.get_asset_price", side_effect=ValueError("stale")),
        patch("src.api.mm_routes._resolve_nonce") as nonce,
        patch("src.api.mm_routes.get_client") as db,
        pytest.raises(HTTPException) as exc,
    ):
        asyncio.run(submit_quotes(body=body, mm_address=MM_ADDRESS))
    assert exc.value.status_code == 503
    nonce.assert_not_called()
    db.assert_not_called()


def test_routed_quote_metadata_must_match_registered_otoken() -> None:
    quote = QuoteSubmission(**{**_base_quote().model_dump(), "asset": "nvdac"})
    body = QuoteBatchRequest(quotes=[quote])
    identity_db = MagicMock()
    identity_db.table.return_value.select.return_value.in_.return_value.execute.return_value = SimpleNamespace(
        data=[
            {
                "otoken_address": quote.otoken_address,
                "underlying": "0xB2000000000000000000008501b13360000cb2EC",
                "strike_price": quote.strike_price,
                "expiry": quote.expiry,
                "is_put": quote.is_put,
            }
        ]
    )
    with (
        patch("src.api.mm_routes.settings.routed_settlement_publishing_enabled", True),
        patch("src.api.mm_routes.get_asset_price", return_value=(100.0, 1_000)),
        patch("src.api.mm_routes.get_client", return_value=identity_db),
        patch("src.api.mm_routes._resolve_nonce") as nonce,
        pytest.raises(HTTPException) as exc,
    ):
        asyncio.run(submit_quotes(body=body, mm_address=MM_ADDRESS))
    assert exc.value.status_code == 400
    nonce.assert_not_called()


def test_submit_quotes_discards_all_write_representations() -> None:
    db = MagicMock()
    body = QuoteBatchRequest(quotes=[_base_quote()])

    with (
        patch("src.api.mm_routes.time.time", return_value=1_000),
        patch(
            "src.api.mm_routes._resolve_nonce",
            return_value=(MM_ADDRESS, 3),
        ),
        patch("src.api.mm_routes._verify_base_sig", return_value=True),
        patch("src.api.mm_routes.get_client", return_value=db),
    ):
        result = asyncio.run(submit_quotes(body=body, mm_address=MM_ADDRESS))

    assert result.accepted == 1
    db.table.return_value.update.assert_called_once_with(
        {"is_active": False}, returning=ReturnMethod.minimal
    )
    upsert_kwargs = db.table.return_value.upsert.call_args.kwargs
    assert upsert_kwargs == {
        "on_conflict": "mm_address,quote_id",
        "returning": ReturnMethod.minimal,
    }
    db.table.return_value.delete.assert_called_once_with(returning=ReturnMethod.minimal)


def test_prune_stale_quotes_uses_minimal_return() -> None:
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


def test_capacity_write_uses_minimal_return() -> None:
    db = MagicMock()
    body = CapacityUpdateRequest(
        asset="eth",
        capacity_eth=2,
        capacity_usd=5_000,
        status="active",
    )

    with (
        patch("src.api.mm_routes.time.time", return_value=1_000),
        patch("src.api.mm_routes.get_client", return_value=db),
    ):
        result = asyncio.run(report_capacity(body=body, mm_address=MM_ADDRESS))

    assert result == {"status": "ok"}
    kwargs = db.table.return_value.upsert.call_args.kwargs
    assert kwargs == {
        "on_conflict": "mm_address,asset",
        "returning": ReturnMethod.minimal,
    }
