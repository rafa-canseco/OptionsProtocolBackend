from src.pricing.circuit_breaker import CircuitBreaker


def test_initial_state():
    cb = CircuitBreaker()
    assert not cb.is_paused
    assert not cb.is_paused_for("eth")


def test_first_check_sets_reference():
    cb = CircuitBreaker()
    tripped = cb.check(2000.0, "eth")
    assert not tripped
    state = cb._get("eth")
    assert state.reference_price == 2000.0


def test_small_move_no_trip():
    cb = CircuitBreaker()
    cb.update_reference(2000.0, "eth")
    assert not cb.check(2010.0, "eth")  # 0.5% move
    assert not cb.check(1990.0, "eth")  # 0.5% move
    assert not cb.check(2039.0, "eth")  # 1.95% — just under


def test_large_move_trips():
    cb = CircuitBreaker()
    cb.update_reference(2000.0, "eth")
    assert cb.check(2040.01, "eth")  # just over 2%
    assert cb.is_paused_for("eth")
    assert cb.pause_reason_for("eth") is not None


def test_resume_resets():
    cb = CircuitBreaker()
    cb.update_reference(2000.0, "eth")
    cb.check(2100.0, "eth")  # trip
    assert cb.is_paused_for("eth")

    cb.resume(2100.0, "eth")
    assert not cb.is_paused_for("eth")
    assert cb._get("eth").reference_price == 2100.0


def test_downward_move_trips():
    cb = CircuitBreaker()
    cb.update_reference(2000.0, "eth")
    assert cb.check(1959.0, "eth")  # -2.05%
    assert cb.is_paused_for("eth")


def test_assets_are_independent():
    """ETH trip does not affect BTC and vice versa."""
    cb = CircuitBreaker()
    cb.update_reference(2000.0, "eth")
    cb.update_reference(70000.0, "btc")

    # ETH trips
    assert cb.check(2100.0, "eth")
    assert cb.is_paused_for("eth")
    assert not cb.is_paused_for("btc")

    # BTC with normal move doesn't trip
    assert not cb.check(70500.0, "btc")
    assert not cb.is_paused_for("btc")


def test_cross_asset_no_false_trip():
    """BTC price checked against ETH reference must not happen."""
    cb = CircuitBreaker()
    cb.update_reference(2000.0, "eth")
    cb.update_reference(70000.0, "btc")

    # Each asset only compares against its own reference
    assert not cb.check(2010.0, "eth")
    assert not cb.check(71000.0, "btc")
