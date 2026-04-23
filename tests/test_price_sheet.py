from src.pricing.price_sheet import generate_otoken_specs, generate_strikes
from src.pricing.utils import get_expiries


def test_generate_strikes_centered():
    strikes = generate_strikes(2086.0, num_strikes=5, min_otm_per_side=0)
    assert len(strikes) == 5
    # Should be centered around 2100 (nearest $50)
    assert strikes[2] == 2100.0


def test_generate_strikes_spacing():
    strikes = generate_strikes(2000.0, num_strikes=5, min_otm_per_side=0)
    for i in range(1, len(strikes)):
        assert strikes[i] - strikes[i - 1] == 50.0


def test_generate_strikes_min_otm_puts():
    """Spot near top of range should still have 4 OTM puts."""
    strikes = generate_strikes(2200.0, step=50.0, num_strikes=5, min_otm_per_side=4)
    otm_puts = [s for s in strikes if s < 2200.0]
    assert len(otm_puts) >= 4


def test_generate_strikes_min_otm_calls():
    """Spot near bottom of range should still have 4 OTM calls."""
    strikes = generate_strikes(1800.0, step=50.0, num_strikes=5, min_otm_per_side=4)
    otm_calls = [s for s in strikes if s > 1800.0]
    assert len(otm_calls) >= 4


def test_generate_strikes_min_otm_both_sides():
    """Both sides guaranteed regardless of spot position."""
    for spot in [1500.0, 2000.0, 2500.0, 3000.0]:
        strikes = generate_strikes(spot, step=50.0, num_strikes=5, min_otm_per_side=4)
        otm_puts = [s for s in strikes if s < spot]
        otm_calls = [s for s in strikes if s > spot]
        assert len(otm_puts) >= 4, f"spot={spot}: only {len(otm_puts)} OTM puts"
        assert len(otm_calls) >= 4, f"spot={spot}: only {len(otm_calls)} OTM calls"


def test_otoken_specs_default_expiries():
    specs = generate_otoken_specs(spot=2000.0)
    expiry_ts_set = {s.expiry_ts for s in specs}
    assert len(expiry_ts_set) >= 3  # 1d + near_fri + 7d + 14d (dedup may collapse)
    for ts in expiry_ts_set:
        assert ts % 86400 == 28800, f"{ts} is not 08:00 UTC"


def test_otoken_specs_both_types():
    specs = generate_otoken_specs(spot=2000.0, num_strikes=1)
    types = {s.option_type.value for s in specs}
    assert types == {"call", "put"}


def test_otoken_specs_count():
    specs = generate_otoken_specs(spot=2000.0)
    num_expiries = len({s.expiry_ts for s in specs})
    assert num_expiries >= 3
    # Each expiry has its own strike count (1d uses tighter steps)
    for ts in {s.expiry_ts for s in specs}:
        expiry_specs = [s for s in specs if s.expiry_ts == ts]
        strikes = {s.strike for s in expiry_specs}
        types = {s.option_type for s in expiry_specs}
        assert len(strikes) >= 5
        assert len(types) == 2
        assert len(expiry_specs) == len(strikes) * len(types)


def test_1day_slot_uses_tighter_strike_step():
    """The 1-day (smallest) expiry uses $25 steps, others use $50."""
    specs = generate_otoken_specs(spot=2000.0)
    expiry_set = sorted({s.expiry_ts for s in specs})
    assert len(expiry_set) >= 2

    daily_ts = expiry_set[0]
    standard_ts = expiry_set[-1]

    daily_strikes = sorted({s.strike for s in specs if s.expiry_ts == daily_ts})
    standard_strikes = sorted({s.strike for s in specs if s.expiry_ts == standard_ts})

    # Daily should have tighter spacing ($25)
    daily_diffs = {
        daily_strikes[i + 1] - daily_strikes[i] for i in range(len(daily_strikes) - 1)
    }
    assert 25.0 in daily_diffs, f"Expected $25 step in daily, got {daily_diffs}"

    # Standard should have wider spacing ($50)
    standard_diffs = {
        standard_strikes[i + 1] - standard_strikes[i]
        for i in range(len(standard_strikes) - 1)
    }
    assert 50.0 in standard_diffs, (
        f"Expected $50 step in standard, got {standard_diffs}"
    )
    assert 25.0 not in standard_diffs, "Standard expiry should not have $25 steps"


def test_otoken_specs_custom_expiries():
    ts1 = get_expiries()[0]
    ts2 = get_expiries()[1]
    specs = generate_otoken_specs(spot=2000.0, expiry_timestamps=[ts1, ts2])
    expiry_ts_set = {s.expiry_ts for s in specs}
    assert expiry_ts_set == {ts1, ts2}


def test_otoken_specs_strikes_around_spot():
    ts = get_expiries()[0]
    specs = generate_otoken_specs(spot=2500.0, expiry_timestamps=[ts], num_strikes=5)
    strikes = {s.strike for s in specs}
    assert 2500.0 in strikes  # center strike
    # At least 5 base + extras from min_otm_per_side
    assert len(strikes) >= 5
    assert len(specs) == len(strikes) * 2  # each strike has call + put
