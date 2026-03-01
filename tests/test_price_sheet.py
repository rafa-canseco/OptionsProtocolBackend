from src.pricing.price_sheet import generate_otoken_specs, generate_strikes


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
    expiry_days = {s.expiry_days for s in specs}
    assert expiry_days == {7, 14, 30}


def test_otoken_specs_both_types():
    specs = generate_otoken_specs(spot=2000.0, num_strikes=1)
    types = {s.option_type.value for s in specs}
    assert types == {"call", "put"}


def test_otoken_specs_count():
    # 5 strikes x 3 expiries x 2 types = 30
    specs = generate_otoken_specs(spot=2000.0)
    assert len(specs) == 5 * 3 * 2


def test_otoken_specs_custom_expiries():
    specs = generate_otoken_specs(spot=2000.0, expiry_days=[1, 3])
    expiry_days = {s.expiry_days for s in specs}
    assert expiry_days == {1, 3}


def test_otoken_specs_strikes_around_spot():
    specs = generate_otoken_specs(spot=2500.0, expiry_days=[7], num_strikes=5)
    assert len(specs) == 10  # 5 strikes x 2 types
    strikes = {s.strike for s in specs}
    assert 2500.0 in strikes  # center strike
