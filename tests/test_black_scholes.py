import math

from src.pricing.black_scholes import OptionType, price, delta, gamma, theta, vega


# Test against known BS values
# S=100, K=100, T=1yr, r=5%, sigma=20% → Call ≈ 10.4506, Put ≈ 5.5735
S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.20


def test_call_price():
    p = price(OptionType.CALL, S, K, T, r, sigma)
    assert abs(p - 10.4506) < 0.01


def test_put_price():
    p = price(OptionType.PUT, S, K, T, r, sigma)
    assert abs(p - 5.5735) < 0.01


def test_put_call_parity():
    """C - P = S - K*exp(-rT)"""
    c = price(OptionType.CALL, S, K, T, r, sigma)
    p = price(OptionType.PUT, S, K, T, r, sigma)
    parity = S - K * math.exp(-r * T)
    assert abs((c - p) - parity) < 1e-10


def test_call_delta_atm():
    d = delta(OptionType.CALL, S, K, T, r, sigma)
    # ATM call delta slightly above 0.5 due to drift
    assert 0.5 < d < 0.7


def test_put_delta_atm():
    d = delta(OptionType.PUT, S, K, T, r, sigma)
    # ATM put delta is call_delta - 1, so roughly -0.36 with drift
    assert -0.2 > d > -0.8


def test_gamma_positive():
    g = gamma(S, K, T, r, sigma)
    assert g > 0


def test_theta_negative_for_calls():
    t = theta(OptionType.CALL, S, K, T, r, sigma)
    assert t < 0  # options lose value over time


def test_vega_positive():
    v = vega(S, K, T, r, sigma)
    assert v > 0


def test_expiry_call_itm():
    p = price(OptionType.CALL, 110.0, 100.0, 0.0, r, sigma)
    assert p == 10.0


def test_expiry_call_otm():
    p = price(OptionType.CALL, 90.0, 100.0, 0.0, r, sigma)
    assert p == 0.0


def test_expiry_put_itm():
    p = price(OptionType.PUT, 90.0, 100.0, 0.0, r, sigma)
    assert p == 10.0


def test_short_dated_eth_option():
    """Realistic ETH option: 7 day, ATM, 80% IV"""
    eth_price = 2700.0
    p = price(OptionType.CALL, eth_price, eth_price, 7 / 365, 0.05, 0.80)
    # Should be roughly 3-5% of spot for short-dated high-IV
    assert 50 < p < 200


def test_put_call_parity_eth():
    """Put-call parity for ETH-like params"""
    S_eth, K_eth, T_eth = 2700.0, 2800.0, 30 / 365
    c = price(OptionType.CALL, S_eth, K_eth, T_eth, 0.05, 0.80)
    p = price(OptionType.PUT, S_eth, K_eth, T_eth, 0.05, 0.80)
    parity = S_eth - K_eth * math.exp(-0.05 * T_eth)
    assert abs((c - p) - parity) < 1e-8
