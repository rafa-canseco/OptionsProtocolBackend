import time

from src.bots.price_publisher import (
    premium_to_usdc,
    strike_to_8_decimals,
    expiry_days_to_timestamp,
    compute_params_hash,
    SPREAD,
)


def test_premium_to_usdc():
    assert premium_to_usdc(100.0) == 100_000_000
    assert premium_to_usdc(0.50) == 500_000
    assert premium_to_usdc(0.000001) == 1  # minimum 1 unit


def test_premium_to_usdc_zero():
    # Zero premium should return minimum 1
    assert premium_to_usdc(0.0) == 1


def test_strike_to_8_decimals():
    assert strike_to_8_decimals(2000.0) == 200_000_000_000
    assert strike_to_8_decimals(2500.50) == 250_050_000_000
    assert strike_to_8_decimals(100.0) == 10_000_000_000


def test_expiry_days_to_timestamp_is_0800_utc():
    """Expiry timestamp must be at 08:00 UTC (ts % 86400 == 28800)."""
    for days in [7, 14, 30]:
        ts = expiry_days_to_timestamp(days)
        assert ts % 86400 == 28800, f"days={days}: {ts} is not 08:00 UTC"


def test_expiry_days_to_timestamp_is_future():
    """All expiry timestamps must be in the future."""
    now = int(time.time())
    for days in [7, 14, 30]:
        ts = expiry_days_to_timestamp(days)
        assert ts > now, f"days={days}: {ts} should be > {now}"


def test_expiry_days_ordering():
    """Longer expiry → later timestamp."""
    ts7 = expiry_days_to_timestamp(7)
    ts14 = expiry_days_to_timestamp(14)
    ts30 = expiry_days_to_timestamp(30)
    assert ts7 < ts14 < ts30


def test_compute_params_hash_deterministic():
    """Same inputs → same hash."""
    args = (
        "0x4200000000000000000000000000000000000006",
        "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        200_000_000_000,
        1771833600,
        True,
    )
    h1 = compute_params_hash(*args)
    h2 = compute_params_hash(*args)
    assert h1 == h2
    assert len(h1) == 32


def test_compute_params_hash_different_for_put_vs_call():
    """Put and call with same strike/expiry should produce different hashes."""
    common = (
        "0x4200000000000000000000000000000000000006",
        "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        200_000_000_000,
        1771833600,
    )
    h_put = compute_params_hash(*common, True)
    h_call = compute_params_hash(*common, False)
    assert h_put != h_call
