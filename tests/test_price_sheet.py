from src.pricing.price_sheet import generate_price_sheet, generate_strikes


def test_generate_strikes_centered():
    strikes = generate_strikes(2086.0, num_strikes=5)
    assert len(strikes) == 5
    # Should be centered around 2100 (nearest $50)
    assert strikes[2] == 2100.0


def test_generate_strikes_spacing():
    strikes = generate_strikes(2000.0, num_strikes=5)
    for i in range(1, len(strikes)):
        assert strikes[i] - strikes[i - 1] == 50.0


def test_price_sheet_default_expiries():
    quotes = generate_price_sheet(spot=2000.0, iv=0.50)
    expiry_days = {q.expiry_days for q in quotes}
    assert expiry_days == {7, 14, 30}


def test_price_sheet_both_types():
    quotes = generate_price_sheet(spot=2000.0, iv=0.50, num_strikes=1)
    types = {q.option_type.value for q in quotes}
    assert types == {"call", "put"}


def test_price_sheet_count():
    # 5 strikes × 3 expiries × 2 types = 30
    quotes = generate_price_sheet(spot=2000.0, iv=0.50)
    assert len(quotes) == 5 * 3 * 2


def test_price_sheet_premiums_positive():
    quotes = generate_price_sheet(spot=2000.0, iv=0.50)
    for q in quotes:
        assert q.premium >= 0


def test_price_sheet_ttl():
    quotes = generate_price_sheet(spot=2000.0, iv=0.50)
    for q in quotes:
        assert q.ttl == 30  # default from config
        assert not q.is_expired  # just created


def test_price_sheet_custom_expiries():
    quotes = generate_price_sheet(spot=2000.0, iv=0.50, expiry_days=[1, 3])
    expiry_days = {q.expiry_days for q in quotes}
    assert expiry_days == {1, 3}


def test_7d_expiry_premiums_positive():
    """7d options should produce valid (positive) premiums at realistic IV."""
    quotes = generate_price_sheet(
        spot=2500.0, iv=0.80, expiry_days=[7], num_strikes=5
    )
    assert len(quotes) == 10  # 5 strikes × 2 types
    for q in quotes:
        assert q.expiry_days == 7
        assert q.premium >= 0
