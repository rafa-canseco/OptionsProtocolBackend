from datetime import datetime, timezone

import pytest

from src.bots.otoken_manager import _with_fund_csp_series
from src.config import settings
from src.pricing.black_scholes import OptionType
from src.pricing.price_sheet import OTokenSpec
from src.pricing.utils import csp_put_strike, get_csp_expiry


def test_csp_expiry_is_first_0800_inside_window():
    now = datetime(2026, 7, 24, 23, 30, tzinfo=timezone.utc)

    expiry = get_csp_expiry(now, min_delay_hours=36, max_delay_hours=60)

    assert expiry == int(datetime(2026, 7, 27, 8, tzinfo=timezone.utc).timestamp())


def test_csp_expiry_rejects_window_without_valid_series():
    now = datetime(2026, 7, 24, 23, 30, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="contains no"):
        get_csp_expiry(now, min_delay_hours=1, max_delay_hours=2)


def test_csp_expiry_accepts_0800_at_exact_lower_bound():
    now = datetime(2026, 7, 24, 20, tzinfo=timezone.utc)

    expiry = get_csp_expiry(now, min_delay_hours=36, max_delay_hours=60)

    assert expiry == int(datetime(2026, 7, 26, 8, tzinfo=timezone.utc).timestamp())


def test_csp_put_strike_is_15_percent_otm_and_rounded_down():
    assert csp_put_strike(3_713.0, otm_bps=1500, tick=25.0) == 3_150.0


def test_csp_put_strike_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        csp_put_strike(0)
    with pytest.raises(ValueError):
        csp_put_strike(2_000, otm_bps=10_000)
    with pytest.raises(ValueError):
        csp_put_strike(2_000, tick=0)


def test_csp_series_gate_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "fund_csp_series_enabled", False)
    specs = [
        OTokenSpec(
            option_type=OptionType.PUT,
            strike=2_000,
            expiry_ts=int(datetime(2026, 8, 1, 8, tzinfo=timezone.utc).timestamp()),
        )
    ]

    assert _with_fund_csp_series(specs, 2_100) is specs


def test_csp_series_adds_one_put_and_deduplicates(monkeypatch):
    monkeypatch.setattr(settings, "fund_csp_series_enabled", True)
    monkeypatch.setattr(settings, "fund_csp_strike_otm_bps", 1500)
    monkeypatch.setattr(settings, "fund_csp_min_expiry_delay_hours", 36)
    monkeypatch.setattr(settings, "fund_csp_max_expiry_delay_hours", 60)
    now = datetime(2026, 7, 24, 23, 30, tzinfo=timezone.utc)

    once = _with_fund_csp_series([], 3_713.0, now=now)
    twice = _with_fund_csp_series(once, 3_713.0, now=now)

    assert once == twice
    assert len(once) == 1
    assert once[0].option_type == OptionType.PUT
    assert once[0].strike == 3_150.0
    assert once[0].expiry_ts == int(
        datetime(2026, 7, 27, 8, tzinfo=timezone.utc).timestamp()
    )
