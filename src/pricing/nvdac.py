"""Authoritative XNYS session calendar used by NVDAc settlement."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

_EASTERN = ZoneInfo("America/New_York")
_MAX_CLOSE_AGE = timedelta(hours=96)


@dataclass(frozen=True)
class CloseWindow:
    close_at: int
    next_session_open_at: int
    reference_price_8: int


def _schedule(start: datetime, end: datetime):
    """Load the maintained XNYS schedule; errors leave callers fail-closed."""
    return xcals.get_calendar(
        "XNYS",
        start=start.date().isoformat(),
        end=end.date().isoformat(),
    ).schedule


def is_us_regular_session(at: datetime | None = None) -> bool:
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Market-session timestamp must be timezone-aware")
    try:
        schedule = _schedule(at - timedelta(days=1), at + timedelta(days=1))
        rows = schedule[schedule.index.date == at.astimezone(_EASTERN).date()]
        if len(rows) != 1 or schedule.index.has_duplicates:
            return False
        session = rows.iloc[0]
        current = at.astimezone(timezone.utc)
        return (
            session["open"].to_pydatetime()
            <= current
            < session["close"].to_pydatetime()
        )
    except Exception:
        return False


def get_finalized_close_window(expiry: int, *, now: int | None = None) -> CloseWindow:
    """Resolve one finalized XNYS close and next open, cross-checked with Yahoo."""
    now_dt = (
        datetime.fromtimestamp(now, timezone.utc)
        if now is not None
        else datetime.now(timezone.utc)
    )
    expiry_dt = datetime.fromtimestamp(expiry, timezone.utc)
    if expiry_dt > now_dt:
        raise ValueError("NVDAc expiry settlement requested before expiry")

    schedule = _schedule(expiry_dt - timedelta(days=8), expiry_dt + timedelta(days=8))
    if schedule.empty or schedule.index.has_duplicates:
        raise ValueError("XNYS close schedule is missing or ambiguous")
    candidates = schedule[schedule["close"] < expiry_dt]
    if candidates.empty:
        raise ValueError("No official NVDA close found within the settlement window")
    close_at = candidates.iloc[-1]["close"].to_pydatetime()
    next_sessions = schedule[schedule["open"] > close_at]
    if next_sessions.empty:
        raise ValueError("No next NVDA session found within the settlement window")
    next_open = next_sessions.iloc[0]["open"].to_pydatetime()
    if next_open <= expiry_dt:
        raise ValueError("Latest NVDA close window does not span expiry")
    if now_dt - close_at > _MAX_CLOSE_AGE:
        raise ValueError("NVDA official close age exceeds 96h")
    if close_at > now_dt:
        raise ValueError("NVDA official close is not finalized")
    if now_dt >= next_open:
        raise ValueError("NVDA official close expired at the next session open")

    # Independent close/finality check. The on-chain price remains Chainlink.
    import yfinance as yf

    close_day = close_at.astimezone(_EASTERN).date()
    history = yf.Ticker("NVDA").history(
        start=close_day.isoformat(),
        end=(close_day + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=False,
    )
    matches = [
        value
        for index, value in zip(history.index, history["Close"], strict=True)
        if index.date() == close_day
    ]
    if len(matches) != 1:
        raise ValueError(
            f"NVDA official close missing or ambiguous for {close_day}: "
            f"{len(matches)} rows"
        )
    close = float(matches[0])
    if not math.isfinite(close) or close <= 0:
        raise ValueError("NVDA official close cross-check is invalid")
    reference_price_8 = int(
        (Decimal(str(close)) * Decimal(10**8)).to_integral_value(rounding=ROUND_HALF_UP)
    )
    return CloseWindow(
        int(close_at.timestamp()),
        int(next_open.timestamp()),
        reference_price_8,
    )
