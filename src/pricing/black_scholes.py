import math
from enum import Enum

from scipy.stats import norm


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


def d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))


def d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def price(
    option_type: OptionType,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
) -> float:
    """
    Black-Scholes option price.

    Args:
        option_type: CALL or PUT
        S: Current spot price of underlying
        K: Strike price
        T: Time to expiry in years (e.g. 7/365 for 7 days)
        r: Risk-free rate (annualized, e.g. 0.05 for 5%)
        sigma: Implied volatility (annualized, e.g. 0.80 for 80%)

    Returns:
        Option premium in same units as S
    """
    if T <= 0:
        # At expiry: intrinsic value only
        if option_type == OptionType.CALL:
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    _d1 = d1(S, K, T, r, sigma)
    _d2 = d2(S, K, T, r, sigma)

    if option_type == OptionType.CALL:
        return S * norm.cdf(_d1) - K * math.exp(-r * T) * norm.cdf(_d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-_d2) - S * norm.cdf(-_d1)


def delta(
    option_type: OptionType,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
) -> float:
    if T <= 0:
        if option_type == OptionType.CALL:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0

    _d1 = d1(S, K, T, r, sigma)
    if option_type == OptionType.CALL:
        return norm.cdf(_d1)
    return norm.cdf(_d1) - 1.0


def gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    return norm.pdf(_d1) / (S * sigma * math.sqrt(T))


def theta(
    option_type: OptionType,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
) -> float:
    """Daily theta (per calendar day)."""
    if T <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    _d2 = d2(S, K, T, r, sigma)

    common = -(S * norm.pdf(_d1) * sigma) / (2 * math.sqrt(T))

    if option_type == OptionType.CALL:
        annual = common - r * K * math.exp(-r * T) * norm.cdf(_d2)
    else:
        annual = common + r * K * math.exp(-r * T) * norm.cdf(-_d2)

    return annual / 365.0


def vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per 1% move in IV."""
    if T <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    return S * norm.pdf(_d1) * math.sqrt(T) / 100.0
