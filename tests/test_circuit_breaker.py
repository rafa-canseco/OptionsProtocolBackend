from src.pricing.circuit_breaker import CircuitBreaker


def test_initial_state():
    cb = CircuitBreaker()
    assert not cb.is_paused
    assert cb.reference_price is None


def test_first_check_sets_reference():
    cb = CircuitBreaker()
    tripped = cb.check(2000.0)
    assert not tripped
    assert cb.reference_price == 2000.0


def test_small_move_no_trip():
    cb = CircuitBreaker()
    cb.update_reference(2000.0)
    assert not cb.check(2010.0)  # 0.5% move
    assert not cb.check(1990.0)  # 0.5% move
    assert not cb.check(2039.0)  # 1.95% — just under


def test_large_move_trips():
    cb = CircuitBreaker()
    cb.update_reference(2000.0)
    assert cb.check(2040.01)  # just over 2%
    assert cb.is_paused
    assert cb.pause_reason is not None


def test_resume_resets():
    cb = CircuitBreaker()
    cb.update_reference(2000.0)
    cb.check(2100.0)  # trip
    assert cb.is_paused

    cb.resume(2100.0)
    assert not cb.is_paused
    assert cb.reference_price == 2100.0


def test_downward_move_trips():
    cb = CircuitBreaker()
    cb.update_reference(2000.0)
    assert cb.check(1959.0)  # -2.05%
    assert cb.is_paused
