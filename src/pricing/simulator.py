import logging

from src.config import settings
from src.models.simulate import ComparisonData, SimulateResponse
from src.pricing import black_scholes as bs
from src.pricing.historical import PricePoint

logger = logging.getLogger(__name__)


def simulate_pnl(
    strike: float,
    spot_history: list[PricePoint],
    iv: float,
) -> SimulateResponse:
    """Simulate selling a cash-secured put over the historical price window.

    Args:
        strike: user-chosen strike price (USD)
        spot_history: daily ETH prices (chronological, at least 2 points)
        iv: current implied volatility (annualized decimal, e.g. 0.80)

    Returns:
        SimulateResponse with premium, assignment status, and comparisons.
    """
    if len(spot_history) < 2:
        raise ValueError("Need at least 2 price points for simulation")

    eth_open = spot_history[0].price
    eth_close = spot_history[-1].price
    eth_low = min(p.price for p in spot_history)

    days = len(spot_history) - 1
    T = days / 365.0

    # Premium: BS put price at the start of the window
    premium = bs.price(
        option_type=bs.OptionType.PUT,
        S=eth_open,
        K=strike,
        T=T,
        r=settings.risk_free_rate,
        sigma=iv,
    )

    # Apply protocol fee
    fee_mult = (10_000 - settings.protocol_fee_bps) / 10_000
    premium_net = premium * fee_mult

    # Assignment: put is ITM if ETH closed below strike
    was_assigned = eth_close < strike

    # Comparisons (all as fraction of notional = strike in USD for a CSP)
    notional = strike  # CSP: collateral = strike * quantity, quantity=1
    hold_return = (eth_close - eth_open) / eth_open if eth_open > 0 else 0.0
    stake_return = settings.eth_staking_apy * (days / 365.0)

    # DCA: buy equal USD amounts daily, compare final value to total spent
    daily_investment = notional / days if days > 0 else 0.0
    total_eth_bought = 0.0
    for p in spot_history[:-1]:  # buy at each day's price (exclude last = "today")
        if p.price > 0:
            total_eth_bought += daily_investment / p.price
    dca_value = total_eth_bought * eth_close
    total_invested = daily_investment * days
    dca_return = (dca_value - total_invested) / total_invested if total_invested > 0 else 0.0

    return SimulateResponse(
        premium_earned=round(premium_net, 2),
        was_assigned=was_assigned,
        eth_low_of_week=round(eth_low, 2),
        eth_close=round(eth_close, 2),
        eth_open=round(eth_open, 2),
        strike=strike,
        comparison=ComparisonData(
            hold_return=round(hold_return, 4),
            stake_return=round(stake_return, 4),
            dca_return=round(dca_return, 4),
        ),
    )
