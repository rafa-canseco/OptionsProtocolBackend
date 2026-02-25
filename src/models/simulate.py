from pydantic import BaseModel, Field


class ComparisonData(BaseModel):
    hold_return: float = Field(description="Fractional return from holding ETH (0.05 = 5%)", examples=[0.05])
    stake_return: float = Field(description="Fractional return from staking ETH (0.05 = 5%)", examples=[0.04])
    dca_return: float = Field(description="Fractional return from daily DCA into ETH (0.05 = 5%)", examples=[0.03])


class SimulateResponse(BaseModel):
    premium_earned: float = Field(description="USD premium earned for 1-contract put (net of protocol fee)", examples=[42.15])
    was_assigned: bool = Field(description="Whether ETH closed below strike (put was assigned)", examples=[False])
    eth_low_of_week: float = Field(description="Lowest ETH price during the simulation period (USD)", examples=[2480.0])
    eth_close: float = Field(description="ETH closing price at end of simulation (USD)", examples=[2650.0])
    eth_open: float = Field(description="ETH opening price at start of simulation (USD)", examples=[2600.0])
    strike: float = Field(description="Strike price used (rounded to nearest $50)", examples=[2400.0])
    comparison: ComparisonData = Field(description="Returns from alternative strategies over the same period")


class WeeklyReport(BaseModel):
    week_start: str = Field(description="Start of the week (ISO date)", examples=["2026-02-17"])
    week_end: str = Field(description="End of the week (ISO date)", examples=["2026-02-23"])
    total_users: int = Field(description="Number of unique users who traded this week", examples=[12])
    total_positions: int = Field(description="Total positions opened this week", examples=[34])
    total_simulated_premium: float = Field(description="Aggregate simulated premium earned (USD)", examples=[1250.0])
    total_assignments: int = Field(description="Number of positions that were assigned", examples=[3])
    eth_open: float = Field(description="ETH price at week open (USD)", examples=[2600.0])
    eth_close: float = Field(description="ETH price at week close (USD)", examples=[2650.0])
    eth_high: float = Field(description="Highest ETH price during the week (USD)", examples=[2750.0])
    eth_low: float = Field(description="Lowest ETH price during the week (USD)", examples=[2480.0])
    narrative_data: dict = Field(description="Additional context for generating weekly narrative summaries")


class UserWeeklyResult(BaseModel):
    user_address: str = Field(description="Ethereum address (lowercase)", examples=["0xabcdef0123456789abcdef0123456789abcdef01"])
    week_start: str = Field(description="Start of the week (ISO date)", examples=["2026-02-17"])
    week_end: str = Field(description="End of the week (ISO date)", examples=["2026-02-23"])
    positions_opened: int = Field(description="Number of positions opened this week", examples=[3])
    total_simulated_premium: float = Field(description="Simulated premium earned this week (USD)", examples=[126.45])
    assignments: int = Field(description="Number of positions assigned this week", examples=[0])
    simulated_pnl: float = Field(description="Net P&L for this week (USD, can be negative)", examples=[126.45])
    cumulative_pnl: float = Field(description="Running total P&L across all weeks (USD)", examples=[450.30])


class EarningsSnapshot(BaseModel):
    week_start: str = Field(description="Start of the week (ISO date)", examples=["2026-01-06"])
    week_end: str = Field(description="End of the week (ISO date)", examples=["2026-01-12"])
    premium_earned: float = Field(description="Premium earned this week (USD)", examples=[124.50])
    assignments: int = Field(description="Number of positions assigned this week", examples=[0])
    pnl: float = Field(description="Net P&L for this week (USD, can be negative)", examples=[124.50])
    cumulative_pnl: float = Field(description="Running total P&L across all weeks (USD)", examples=[124.50])


class UserStats(BaseModel):
    user_address: str = Field(description="Ethereum address (lowercase)", examples=["0xabcdef0123456789abcdef0123456789abcdef01"])
    weeks_active: int = Field(description="Number of weeks with at least one position", examples=[5])
    cumulative_pnl: float = Field(description="Total P&L across all weeks (USD)", examples=[450.30])
    best_week_pnl: float = Field(description="Highest single-week P&L (USD)", examples=[200.0])
    total_premium_earned: float = Field(description="Sum of all simulated premiums earned (USD)", examples=[630.0])
    total_assignments: int = Field(description="Total times positions were assigned", examples=[2])
    total_positions: int = Field(description="Total positions opened across all weeks", examples=[15])
