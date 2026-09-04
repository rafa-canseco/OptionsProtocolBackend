from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.bots.expiry_settler import _ensure_expiry_prices_set
from src.config import settings
from src.pricing.assets import Asset, get_base_settlement_asset
from src.pricing.chainlink import ValidatedChainlinkRound, get_asset_price_raw
from src.pricing.nvdac import (
    CloseWindow,
    get_finalized_close_window,
    is_us_regular_session,
)
from src.settlement_routing import (
    NvdacClosePrice,
    read_new_asset_price_8,
    read_nvdac_close_price_8,
)

_ET = ZoneInfo("America/New_York")


def _ts(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=_ET).timestamp())


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 1, 6, 9, 29, tzinfo=_ET), False),
        (datetime(2026, 1, 6, 9, 30, tzinfo=_ET), True),
        (datetime(2026, 1, 6, 15, 59, tzinfo=_ET), True),
        (datetime(2026, 1, 6, 16, 0, tzinfo=_ET), False),
        (datetime(2026, 1, 10, 12, 0, tzinfo=_ET), False),
        (datetime(2026, 1, 19, 12, 0, tzinfo=_ET), False),  # MLK
        (datetime(2026, 4, 3, 12, 0, tzinfo=_ET), False),  # Good Friday
        (datetime(2026, 6, 19, 12, 0, tzinfo=_ET), False),  # Juneteenth
        (datetime(2026, 7, 3, 12, 0, tzinfo=_ET), False),  # July 4 observed
        (datetime(2026, 11, 27, 12, 59, tzinfo=_ET), True),  # Black Friday
        (datetime(2026, 11, 27, 13, 0, tzinfo=_ET), False),
        (datetime(2026, 12, 24, 12, 59, tzinfo=_ET), True),  # Christmas Eve
        (datetime(2026, 12, 24, 13, 0, tzinfo=_ET), False),
        (datetime(2026, 3, 9, 13, 29, tzinfo=timezone.utc), False),  # DST
        (datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc), True),
        (datetime(2026, 11, 2, 14, 29, tzinfo=timezone.utc), False),
        (datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc), True),
    ],
)
def test_us_regular_session_boundaries_weekend_holiday_and_early_close(at, expected):
    assert is_us_regular_session(at) is expected


def _history(*dates_and_closes):
    return pd.DataFrame(
        {"Close": [close for _, close in dates_and_closes]},
        index=pd.DatetimeIndex(
            [
                datetime.combine(day, datetime.min.time(), _ET)
                for day, _ in dates_and_closes
            ]
        ),
    )


def test_weekend_expiry_uses_latest_finalized_friday_close_window():
    expiry = _ts(2026, 1, 11, 12)  # Sunday
    ticker = MagicMock()
    ticker.history.return_value = _history((date(2026, 1, 9), 187.25))
    with patch("yfinance.Ticker", return_value=ticker):
        window = get_finalized_close_window(expiry, now=_ts(2026, 1, 11, 13))
    assert window == CloseWindow(
        _ts(2026, 1, 9, 16),
        _ts(2026, 1, 12, 9, 30),
        18_725_000_000,
    )
    assert ticker.history.call_args.kwargs["start"] == "2026-01-09"


@pytest.mark.parametrize(
    "rows", [(), ((date(2026, 1, 9), 187.25), (date(2026, 1, 9), 188.0))]
)
def test_official_close_fails_closed_when_missing_or_ambiguous(rows):
    ticker = MagicMock()
    ticker.history.return_value = _history(*rows)
    with (
        patch("yfinance.Ticker", return_value=ticker),
        pytest.raises(ValueError, match="missing or ambiguous"),
    ):
        get_finalized_close_window(_ts(2026, 1, 11, 12), now=_ts(2026, 1, 11, 13))


def test_close_exactly_at_expiry_fails_closed_for_contract_compatibility():
    expiry_dt = datetime(2026, 1, 9, 21, tzinfo=timezone.utc)
    schedule = pd.DataFrame(
        {
            "open": pd.to_datetime(
                [
                    "2026-01-08T14:30:00Z",
                    "2026-01-09T14:30:00Z",
                    "2026-01-12T14:30:00Z",
                ]
            ),
            "close": pd.to_datetime(
                [
                    "2026-01-08T21:00:00Z",
                    "2026-01-09T21:00:00Z",
                    "2026-01-12T21:00:00Z",
                ]
            ),
        },
        index=pd.DatetimeIndex(["2026-01-08", "2026-01-09", "2026-01-12"]),
    )
    with (
        patch("src.pricing.nvdac._schedule", return_value=schedule),
        pytest.raises(ValueError, match="does not span expiry"),
    ):
        get_finalized_close_window(
            int(expiry_dt.timestamp()),
            now=int((expiry_dt + pd.Timedelta(hours=1)).timestamp()),
        )


def test_official_close_rejects_age_over_96_hours():
    expiry = datetime(2026, 1, 12, 12, tzinfo=timezone.utc)
    schedule = pd.DataFrame(
        {
            "open": pd.to_datetime(["2026-01-07T14:30:00Z", "2026-01-13T14:30:00Z"]),
            "close": pd.to_datetime(["2026-01-07T21:00:00Z", "2026-01-13T21:00:00Z"]),
        },
        index=pd.DatetimeIndex(["2026-01-07", "2026-01-13"]),
    )
    with (
        patch("src.pricing.nvdac._schedule", return_value=schedule),
        pytest.raises(ValueError, match="exceeds 96h"),
    ):
        get_finalized_close_window(int(expiry.timestamp()), now=int(expiry.timestamp()))


def test_nvdac_raw_price_returns_chainlink_tuple_only_in_live_session():
    result = ValidatedChainlinkRound(0, _ts(2026, 1, 6, 10), 8, 19_000_000_000)
    now = _ts(2026, 1, 6, 10, 30)
    with (
        patch("src.pricing.chainlink.get_w3"),
        patch(
            "src.pricing.chainlink.read_validated_chainlink_round",
            return_value=result,
        ) as read,
    ):
        assert get_asset_price_raw(Asset.NVDAC, now=now) == (
            19_000_000_000,
            8,
            result.updated_at,
        )
    assert read.call_args.kwargs["max_age_seconds"] == 3600

    with pytest.raises(ValueError, match="outside session"):
        get_asset_price_raw(Asset.NVDAC, now=_ts(2026, 1, 6, 16))


def test_crypto_raw_price_path_remains_unchanged_by_market_hours():
    feed = MagicMock()
    feed.functions.latestRoundData.return_value.call.return_value = (
        1,
        250_000_000_000,
        1,
        1,
        1,
    )
    with (
        patch("src.pricing.chainlink._get_feed", return_value=feed),
        patch("src.pricing.chainlink._get_decimals", return_value=8),
    ):
        assert get_asset_price_raw(Asset.ETH) == (250_000_000_000, 8, 1)


def test_live_nvdac_expiry_price_can_skip_multiplier_for_oracle_units():
    cfg = get_base_settlement_asset("nvdac")
    route = {
        "source_chain": settings.chain_id,
        "feed": settings.chainlink_nvdac_usd_address,
        "feed_decimals": 8,
        "feed_description": settings.chainlink_nvdac_usd_description,
    }
    result = ValidatedChainlinkRound(999, 10_000, 8, 123)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=2 * 10**18),
        patch("src.settlement_routing.is_us_regular_session", return_value=True),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch(
            "src.settlement_routing.read_validated_chainlink_round", return_value=result
        ),
    ):
        assert (
            read_new_asset_price_8("nvdac", now=10_000, apply_multiplier=False) == 123
        )
        assert read_new_asset_price_8("nvdac", now=10_000) == 246


def test_close_price_uses_exact_chainlink_answer_and_one_hour_close_window():
    cfg = get_base_settlement_asset("nvdac")
    route = {
        "source_chain": settings.chain_id,
        "feed": settings.chainlink_nvdac_usd_address,
        "feed_decimals": 8,
        "feed_description": settings.chainlink_nvdac_usd_description,
    }
    expiry = _ts(2026, 1, 11, 12)
    window = CloseWindow(_ts(2026, 1, 9, 16), _ts(2026, 1, 12, 9, 30), 123)
    round_data = ValidatedChainlinkRound(999, window.close_at, 8, 123)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=2 * 10**18),
        patch("src.settlement_routing.get_finalized_close_window", return_value=window),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch(
            "src.settlement_routing.read_validated_chainlink_round",
            return_value=round_data,
        ) as read,
    ):
        result = read_nvdac_close_price_8(expiry, now=_ts(2026, 1, 11, 13))
    assert result == NvdacClosePrice(
        123, 246, window.close_at, window.next_session_open_at
    )
    assert read.call_args.kwargs["not_before"] == window.close_at - 3600
    assert read.call_args.kwargs["not_after"] == window.close_at + 3600
    assert read.call_args.kwargs["max_age_seconds"] == 96 * 3600


def test_close_price_rejects_yahoo_chainlink_deviation_over_100_bps():
    cfg = get_base_settlement_asset("nvdac")
    route = {
        "source_chain": settings.chain_id,
        "feed": settings.chainlink_nvdac_usd_address,
        "feed_decimals": 8,
        "feed_description": settings.chainlink_nvdac_usd_description,
    }
    window = CloseWindow(1_000, 20_000, 10_000)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=10**18),
        patch("src.settlement_routing.get_finalized_close_window", return_value=window),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch(
            "src.settlement_routing.read_validated_chainlink_round",
            return_value=ValidatedChainlinkRound(0, 1_000, 8, 10_101),
        ),
        pytest.raises(ValueError, match="deviation exceeds 100 bps"),
    ):
        read_nvdac_close_price_8(5_000, now=6_000)


def test_outside_session_close_is_settlement_only():
    now = _ts(2026, 1, 11, 13)
    with patch("src.settlement_routing.is_us_regular_session", return_value=False):
        with pytest.raises(ValueError, match="unavailable outside session"):
            read_new_asset_price_8("nvdac", now=now)


def test_expiry_setter_passes_nvdac_expiry_to_close_policy():
    cfg = get_base_settlement_asset("nvdac")
    oracle = MagicMock()
    oracle.functions.getExpiryPrice.return_value.call.return_value = (0, False)
    expiry = _ts(2026, 1, 11, 12)
    with (
        patch("src.bots.expiry_settler._position_asset_config", return_value=cfg),
        patch("src.bots.expiry_settler.get_oracle", return_value=oracle),
        patch(
            "src.bots.expiry_settler.get_operator_account",
            return_value=SimpleNamespace(),
        ),
        patch("src.bots.expiry_settler.is_us_regular_session", return_value=False),
        patch(
            "src.bots.expiry_settler.read_nvdac_close_price_8",
            return_value=NvdacClosePrice(123, 246, expiry - 3600, expiry + 3600),
        ) as read_close,
        patch("src.bots.expiry_settler.build_and_send_tx", return_value="0xtx"),
    ):
        _ensure_expiry_prices_set({("nvdac", expiry)}, now=expiry + 1)
    oracle.functions.setExpiryPriceFromClose.assert_called_once_with(
        cfg.underlying_address,
        expiry,
        123,
        expiry - 3600,
        expiry + 3600,
    )
    oracle.functions.setExpiryPrice.assert_not_called()
    assert read_close.call_args.kwargs["now"] == expiry + 1
