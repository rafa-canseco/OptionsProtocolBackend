from src.pricing.price_sheet import generate_otoken_specs, generate_strikes
from src.pricing.utils import get_friday_expiries


def test_generate_strikes_centered():
    strikes = generate_strikes(2086.0, num_strikes=5)
    assert len(strikes) == 5
    # Should be centered around 2100 (nearest $50)
    assert strikes[2] == 2100.0


def test_generate_strikes_spacing():
    strikes = generate_strikes(2000.0, num_strikes=5)
    for i in range(1, len(strikes)):
        assert strikes[i] - strikes[i - 1] == 50.0


def test_otoken_specs_default_expiries():
    specs = generate_otoken_specs(spot=2000.0)
    expiry_ts_set = {s.expiry_ts for s in specs}
    assert len(expiry_ts_set) == 3
    for ts in expiry_ts_set:
        assert ts % 86400 == 28800, f"{ts} is not 08:00 UTC"


def test_otoken_specs_both_types():
    specs = generate_otoken_specs(spot=2000.0, num_strikes=1)
    types = {s.option_type.value for s in specs}
    assert types == {"call", "put"}


def test_otoken_specs_count():
    # 5 strikes x 3 expiries x 2 types = 30
    specs = generate_otoken_specs(spot=2000.0)
    assert len(specs) == 5 * 3 * 2


def test_otoken_specs_custom_expiries():
    ts1 = get_friday_expiries()[0]
    ts2 = get_friday_expiries()[1]
    specs = generate_otoken_specs(spot=2000.0, expiry_timestamps=[ts1, ts2])
    expiry_ts_set = {s.expiry_ts for s in specs}
    assert expiry_ts_set == {ts1, ts2}


def test_otoken_specs_strikes_around_spot():
    ts = get_friday_expiries()[0]
    specs = generate_otoken_specs(spot=2500.0, expiry_timestamps=[ts], num_strikes=5)
    assert len(specs) == 10  # 5 strikes x 2 types
    strikes = {s.strike for s in specs}
    assert 2500.0 in strikes  # center strike
