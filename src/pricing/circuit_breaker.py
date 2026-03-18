import time

from src.config import settings


class _AssetState:
    """Per-asset circuit breaker state."""

    __slots__ = (
        "reference_price",
        "reference_time",
        "is_paused",
        "paused_at",
        "pause_reason",
    )

    def __init__(self) -> None:
        self.reference_price: float | None = None
        self.reference_time: float | None = None
        self.is_paused: bool = False
        self.paused_at: float | None = None
        self.pause_reason: str | None = None


class CircuitBreaker:
    """Per-asset price circuit breaker.

    Tracks reference prices independently for each asset so that
    comparing ETH and BTC prices never causes a false trip.
    """

    def __init__(self) -> None:
        self._assets: dict[str, _AssetState] = {}

    def _get(self, asset: str) -> _AssetState:
        key = asset.lower()
        if key not in self._assets:
            self._assets[key] = _AssetState()
        return self._assets[key]

    def update_reference(self, price: float, asset: str = "eth") -> None:
        state = self._get(asset)
        state.reference_price = price
        state.reference_time = time.time()
        state.is_paused = False
        state.paused_at = None
        state.pause_reason = None

    def check(self, current_price: float, asset: str = "eth") -> bool:
        """Return True if pricing should be paused for this asset."""
        state = self._get(asset)

        if state.reference_price is None:
            self.update_reference(current_price, asset)
            return False

        move = abs(current_price - state.reference_price) / state.reference_price

        if move >= settings.circuit_breaker_threshold:
            state.is_paused = True
            state.paused_at = time.time()
            state.pause_reason = (
                f"{asset.upper()} moved {move:.2%} since last update "
                f"(ref: ${state.reference_price:.2f}, "
                f"now: ${current_price:.2f})"
            )
            return True

        return False

    def is_paused_for(self, asset: str) -> bool:
        return self._get(asset).is_paused

    def pause_reason_for(self, asset: str) -> str | None:
        return self._get(asset).pause_reason

    def resume(self, new_reference_price: float, asset: str = "eth") -> None:
        self.update_reference(new_reference_price, asset)

    @property
    def is_paused(self) -> bool:
        """True if ANY asset is paused (backward compat)."""
        return any(s.is_paused for s in self._assets.values())

    @property
    def pause_reason(self) -> str | None:
        """First paused asset's reason (backward compat)."""
        for s in self._assets.values():
            if s.is_paused:
                return s.pause_reason
        return None

    @property
    def status(self) -> dict:
        return {
            asset: {
                "is_paused": s.is_paused,
                "reference_price": s.reference_price,
                "pause_reason": s.pause_reason,
                "paused_at": s.paused_at,
            }
            for asset, s in self._assets.items()
        }


# Singleton instance
circuit_breaker = CircuitBreaker()
