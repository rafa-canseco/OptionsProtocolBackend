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
    _iter_prior_chainlink_round_ids,
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


def test_nvdac_raw_price_uses_adjusted_routed_snapshot():
    updated_at = _ts(2026, 1, 6, 10)
    now = _ts(2026, 1, 6, 10, 30)
    snapshot = SimpleNamespace(price_8=38_000_000_000, updated_at=updated_at)
    with patch(
        "src.settlement_routing.read_new_asset_price_snapshot",
        return_value=snapshot,
    ) as read:
        assert get_asset_price_raw(Asset.NVDAC, now=now) == (
            38_000_000_000,
            8,
            updated_at,
        )
    read.assert_called_once_with("nvdac", now=now)

    with (
        patch(
            "src.settlement_routing.read_new_asset_price_snapshot",
            side_effect=ValueError(
                "NVDAc live Chainlink price unavailable outside session"
            ),
        ),
        pytest.raises(ValueError, match="outside session"),
    ):
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
    round_data = ValidatedChainlinkRound(999, window.close_at, 8, 123, 321)
    previous_round = ValidatedChainlinkRound(998, window.close_at - 1, 8, 123, 320)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=2 * 10**18),
        patch("src.settlement_routing.get_finalized_close_window", return_value=window),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch(
            "src.settlement_routing.read_validated_chainlink_round",
            side_effect=[round_data, previous_round],
        ) as read,
    ):
        result = read_nvdac_close_price_8(expiry, now=_ts(2026, 1, 11, 13))
    assert result == NvdacClosePrice(
        123, 246, window.close_at, window.next_session_open_at, 321
    )
    assert "not_before" not in read.call_args_list[0].kwargs
    assert "not_after" not in read.call_args_list[0].kwargs
    assert read.call_args_list[0].kwargs["max_age_seconds"] == 96 * 3600
    assert read.call_args_list[1].kwargs["round_id"] == 320


def test_round_scan_crosses_chainlink_phase_boundary():
    w3 = MagicMock()
    proxy = MagicMock()
    previous_aggregator = MagicMock()
    proxy.functions.phaseAggregators.return_value.call.return_value = "0x" + "11" * 20
    previous_aggregator.functions.latestRoundData.return_value.call.return_value = (
        3,
        1,
        1,
        1,
        3,
    )
    w3.eth.contract.side_effect = [proxy, previous_aggregator]

    latest_round_id = (2 << 64) | 1
    assert list(
        _iter_prior_chainlink_round_ids(w3, "0x" + "22" * 20, latest_round_id, 3)
    ) == [
        (1 << 64) | 3,
        (1 << 64) | 2,
        (1 << 64) | 1,
    ]


def test_close_price_reads_historical_round_after_latest_round_moves():
    cfg = get_base_settlement_asset("nvdac")
    route = {
        "source_chain": settings.chain_id,
        "feed": settings.chainlink_nvdac_usd_address,
        "feed_decimals": 8,
        "feed_description": settings.chainlink_nvdac_usd_description,
    }
    window = CloseWindow(10_000, 20_000, 1_000)
    latest = ValidatedChainlinkRound(5_000, 15_000, 8, 5_000, 4)
    close_round = ValidatedChainlinkRound(1_000, 10_000, 8, 1_000, 2)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=10**18),
        patch("src.settlement_routing.get_finalized_close_window", return_value=window),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch(
            "src.settlement_routing.read_validated_chainlink_round",
            side_effect=[
                latest,
                ValueError("round unavailable"),
                close_round,
                ValidatedChainlinkRound(999, window.close_at - 1, 8, 1_000, 1),
            ],
        ) as read,
    ):
        result = read_nvdac_close_price_8(5_000, now=6_000)
    assert result.round_id == 2
    assert read.call_args_list[1].kwargs["round_id"] == 3
    assert read.call_args_list[2].kwargs["round_id"] == 2
    assert read.call_args_list[3].kwargs["round_id"] == 1


def test_close_price_uses_first_round_in_close_window():
    cfg = get_base_settlement_asset("nvdac")
    route = {
        "source_chain": settings.chain_id,
        "feed": settings.chainlink_nvdac_usd_address,
        "feed_decimals": 8,
        "feed_description": settings.chainlink_nvdac_usd_description,
    }
    window = CloseWindow(10_000, 20_000, 1_200)
    latest = ValidatedChainlinkRound(1_300, 11_800, 8, 1_300, 4)
    first_close_round = ValidatedChainlinkRound(1_200, 11_200, 8, 1_200, 3)
    previous_round = ValidatedChainlinkRound(1_100, 9_999, 8, 1_100, 2)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=10**18),
        patch("src.settlement_routing.get_finalized_close_window", return_value=window),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch(
            "src.settlement_routing.read_validated_chainlink_round",
            side_effect=[latest, first_close_round, previous_round],
        ),
    ):
        result = read_nvdac_close_price_8(5_000, now=6_000)
    assert result.round_id == 3
    assert result.price_8 == 1_200


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
            side_effect=[
                ValidatedChainlinkRound(0, 1_000, 8, 10_101, 321),
                ValidatedChainlinkRound(0, 999, 8, 10_101, 320),
            ],
        ),
        pytest.raises(ValueError, match="deviation exceeds 100 bps"),
    ):
        read_nvdac_close_price_8(5_000, now=6_000)


def test_outside_session_close_is_settlement_only():
    now = _ts(2026, 1, 11, 13)
    with patch("src.settlement_routing.is_us_regular_session", return_value=False):
        with pytest.raises(ValueError, match="unavailable outside session"):
            read_new_asset_price_8("nvdac", now=now)


def test_expiry_setter_requires_legacy_allowlist_for_generic_assets():
    cfg = get_base_settlement_asset("cbzec")
    oracle = MagicMock()
    oracle.functions.getExpiryPrice.return_value.call.return_value = (0, False)
    oracle.functions.legacyPostExpiryAsset.return_value.call.return_value = True
    expiry = _ts(2026, 1, 11, 12)
    with (
        patch("src.bots.expiry_settler._position_asset_config", return_value=cfg),
        patch("src.bots.expiry_settler.get_oracle", return_value=oracle),
        patch(
            "src.bots.expiry_settler.get_operator_account",
            return_value=SimpleNamespace(),
        ),
        patch(
            "src.bots.expiry_settler.read_new_asset_price_8",
            return_value=123,
        ),
        patch("src.bots.expiry_settler.build_and_send_tx", return_value="0xtx"),
    ):
        _ensure_expiry_prices_set({("cbzec", expiry)}, now=expiry + 1)
    oracle.functions.setExpiryPrice.assert_called_once_with(
        cfg.underlying_address, expiry, 123
    )
    oracle.functions.setExpiryPriceFromCloseAtRound.assert_not_called()


def test_expiry_setter_passes_nvdac_expiry_to_close_policy():
    cfg = get_base_settlement_asset("nvdac")
    oracle = MagicMock()
    oracle.functions.getExpiryPrice.return_value.call.return_value = (0, False)
    expiry = _ts(2026, 1, 11, 12)
    oracle.functions.closeWindow.return_value.call.return_value = (
        expiry - 3600,
        expiry + 3600,
    )
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
            return_value=NvdacClosePrice(123, 246, expiry - 3600, expiry + 3600, 321),
        ) as read_close,
        patch("src.bots.expiry_settler.build_and_send_tx", return_value="0xtx"),
    ):
        _ensure_expiry_prices_set({("nvdac", expiry)}, now=expiry + 1)
    oracle.functions.setExpiryPriceFromCloseAtRound.assert_called_once_with(
        cfg.underlying_address,
        expiry,
        123,
        expiry - 3600,
        expiry + 3600,
        321,
    )
    oracle.functions.setExpiryPriceFromClose.assert_not_called()
    oracle.functions.setExpiryPrice.assert_not_called()
    assert read_close.call_args.kwargs["now"] == expiry + 1
    oracle.functions.closeWindow.assert_called_once_with(cfg.underlying_address, expiry)
