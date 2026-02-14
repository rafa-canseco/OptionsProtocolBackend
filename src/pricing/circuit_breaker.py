import time

from src.config import settings


class CircuitBreaker:
    """Monitors ETH price and pauses quoting if price moves too much."""

    def __init__(self) -> None:
        self.reference_price: float | None = None
        self.reference_time: float | None = None
        self.is_paused: bool = False
        self.paused_at: float | None = None
        self.pause_reason: str | None = None

    def update_reference(self, price: float) -> None:
        """Set a new reference price (called when MM refreshes prices)."""
        self.reference_price = price
        self.reference_time = time.time()
        self.is_paused = False
        self.paused_at = None
        self.pause_reason = None

    def check(self, current_price: float) -> bool:
        """Check if current price triggers the circuit breaker.

        Returns True if pricing should be paused.
        """
        if self.reference_price is None:
            self.update_reference(current_price)
            return False

        move = abs(current_price - self.reference_price) / self.reference_price

        if move >= settings.circuit_breaker_threshold:
            self.is_paused = True
            self.paused_at = time.time()
            self.pause_reason = (
                f"ETH moved {move:.2%} since last update "
                f"(ref: ${self.reference_price:.2f}, now: ${current_price:.2f})"
            )
            return True

        return False

    def resume(self, new_reference_price: float) -> None:
        """Manually resume after a circuit breaker trip."""
        self.update_reference(new_reference_price)

    @property
    def status(self) -> dict:
        return {
            "is_paused": self.is_paused,
            "reference_price": self.reference_price,
            "pause_reason": self.pause_reason,
            "paused_at": self.paused_at,
        }


# Singleton instance
circuit_breaker = CircuitBreaker()
