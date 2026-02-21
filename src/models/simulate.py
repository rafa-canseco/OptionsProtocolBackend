from pydantic import BaseModel


class ComparisonData(BaseModel):
    hold_return: float  # % return from holding ETH
    stake_return: float  # % return from staking ETH
    dca_return: float  # % return from daily DCA


class SimulateResponse(BaseModel):
    premium_earned: float  # USD premium for 1 ETH notional
    was_assigned: bool  # did ETH close below strike (put)?
    eth_low_of_week: float
    eth_close: float
    eth_open: float
    strike: float
    comparison: ComparisonData


class WeeklyReport(BaseModel):
    week_start: str  # ISO date
    week_end: str
    total_users: int
    total_positions: int
    total_simulated_premium: float
    total_assignments: int
    eth_open: float
    eth_close: float
    eth_high: float
    eth_low: float
    narrative_data: dict  # highest_premium, closest_to_assignment, etc.


class UserWeeklyResult(BaseModel):
    user_address: str
    week_start: str
    week_end: str
    positions_opened: int
    total_simulated_premium: float
    assignments: int
    simulated_pnl: float
    cumulative_pnl: float


class UserStats(BaseModel):
    user_address: str
    weeks_active: int
    cumulative_pnl: float
    best_week_pnl: float
    total_premium_earned: float
    total_assignments: int
    total_positions: int
