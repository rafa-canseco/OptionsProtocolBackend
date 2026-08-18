from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from src.bots import otoken_manager
from src.models.series import EnsureSeriesRequest, ExecutionQuoteSnapshot
from src.otokens.models import CanonicalSeries, canonical_series_from_row
from src.pricing.black_scholes import OptionType
from src.pricing.price_sheet import OTokenSpec

FACTORY = "0x1111111111111111111111111111111111111111"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
OTOKEN = "0x2222222222222222222222222222222222222222"
MM = "0x3333333333333333333333333333333333333333"
WALLET = "0x4444444444444444444444444444444444444444"


def make_series(chain_id: int) -> CanonicalSeries:
    return CanonicalSeries(
        chain_id=chain_id,
        factory_address=FACTORY,
        underlying=WETH,
        strike_asset=USDC,
        collateral_asset=USDC,
        strike_price_raw=2000 * 10**8,
        expiry=2_000_000_000,
        is_put=True,
    )


def test_series_key_is_chain_scoped_and_round_trips() -> None:
    mainnet = make_series(8453)
    sepolia = make_series(84532)

    assert mainnet.series_key != sepolia.series_key
    row = mainnet.to_row(
        otoken_address=OTOKEN,
        strike_price=2000.0,
        deployment_status="virtual",
    )
    assert row["chain_id"] == 8453
    assert canonical_series_from_row(row) == mainnet

    row = sepolia.to_row(
        otoken_address=OTOKEN,
        strike_price=2000.0,
        deployment_status="virtual",
    )
    assert row["chain_id"] == 84532
    assert canonical_series_from_row(row) == sepolia


def test_series_wire_model_preserves_decimal_strings() -> None:
    quote = {
        "otoken_address": OTOKEN,
        "bid_price_raw": "1000000",
        "deadline": "2000000000",
        "quote_id": "42",
        "max_amount_raw": "100000000",
        "maker_nonce": "7",
        "signature": "0x" + "ab" * 65,
        "mm_address": MM,
    }
    request = EnsureSeriesRequest(
        wallet_address=WALLET,
        expected_otoken_address=OTOKEN,
        amount_raw="1000000",
        quote=quote,
    )
    assert request.quote.model_dump() == quote
    assert request.amount_raw == "1000000"


@pytest.mark.parametrize("field", ["amount_raw", "deadline", "maker_nonce"])
def test_series_wire_model_rejects_non_decimal_uints(field: str) -> None:
    quote = {
        "otoken_address": OTOKEN,
        "bid_price_raw": "1000000",
        "deadline": "2000000000",
        "quote_id": "42",
        "max_amount_raw": "100000000",
        "maker_nonce": "7",
        "signature": "0x" + "ab" * 65,
        "mm_address": MM,
    }
    payload = {
        "wallet_address": WALLET,
        "expected_otoken_address": OTOKEN,
        "amount_raw": "1000000",
        "quote": quote,
    }
    if field == "amount_raw":
        payload[field] = "1e6"
    else:
        quote[field] = "-1"
    with pytest.raises(ValueError):
        EnsureSeriesRequest.model_validate(payload)


def test_lazy_publish_backfills_current_legacy_row_once(monkeypatch) -> None:
    expiry = int(datetime(2030, 1, 4, 8, 0, tzinfo=timezone.utc).timestamp())
    spec = OTokenSpec(OptionType.PUT, 2000.0, expiry)
    canonical = make_series(8453)
    canonical = CanonicalSeries(
        **{
            **canonical.__dict__,
            "factory_address": FACTORY,
            "expiry": expiry,
        }
    )
    legacy = {
        "id": "legacy-id",
        "series_key": None,
        "otoken_address": OTOKEN,
        "strike_price": "2000",
        "expiry": expiry,
        "is_put": True,
        "deployment_status": "ready",
    }
    upgraded = {**legacy, "series_key": canonical.series_key}
    repository = MagicMock()
    query = repository.client.table.return_value
    execute = query.select.return_value.eq.return_value.eq.return_value.in_.return_value.execute
    execute.side_effect = [
        MagicMock(data=[legacy]),
        MagicMock(data=[upgraded]),
    ]
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN

    monkeypatch.setattr(
        otoken_manager, "_canonical_series", lambda _spec, _asset: canonical
    )
    with (
        patch(
            "src.bots.otoken_manager.SeriesRepository",
            return_value=repository,
        ),
        patch(
            "src.bots.otoken_manager.get_otoken_factory",
            return_value=factory,
        ),
    ):
        assert (
            otoken_manager._publish_virtual_otokens([spec], otoken_manager.Asset.ETH)
            == 0
        )
        assert (
            otoken_manager._publish_virtual_otokens([spec], otoken_manager.Asset.ETH)
            == 0
        )

    assert factory.functions.getTargetOTokenAddress.return_value.call.call_count == 1
    repository.insert_virtual_rows.assert_has_calls([call([]), call([])])


def test_execution_quote_snapshot_uint_conversion() -> None:
    snapshot = ExecutionQuoteSnapshot(
        otoken_address=OTOKEN,
        bid_price_raw="10",
        deadline="20",
        quote_id="30",
        max_amount_raw="40",
        maker_nonce="50",
        signature="0x" + "ab" * 65,
        mm_address=MM,
    )
    assert snapshot.as_ints() == {
        "bid_price": 10,
        "deadline": 20,
        "quote_id": 30,
        "max_amount": 40,
        "maker_nonce": 50,
    }
