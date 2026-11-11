"""Capital allocation across strategies for autonomous portfolios."""

from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime

from .config import AllocationConfig


class CapitalAllocator:
    """Allocate account equity across strategies by weight.

    Two methods are supported:

    * ``equal``: identical weight for every strategy.
    * ``inverse_volatility``: weight proportional to the inverse of the
      equity curve volatility, so calmer strategies receive more capital.
    """

    def __init__(
        self,
        config: AllocationConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create an allocator from allocation settings."""
        self.config: AllocationConfig = config
        self._clock: Callable[[], datetime] = clock or datetime.now
        self._equity: dict[str, deque[float]] = {}
        self._lock: threading.RLock = threading.RLock()

    def register_strategy(self, name: str) -> None:
        """Start tracking equity for a strategy."""
        with self._lock:
            if name not in self._equity:
                self._equity[name] = deque(maxlen=max(self.config.vol_window, 2))

    def record_equity(self, name: str, equity: float) -> None:
        """Record one equity sample for a strategy."""
        with self._lock:
            series: deque[float] = self._equity.setdefault(
                name, deque(maxlen=max(self.config.vol_window, 2))
            )
            series.append(equity)

    def equity_returns(self, name: str) -> list[float]:
        """Return the simple return series of a strategy equity curve."""
        with self._lock:
            series: deque[float] = self._equity.get(
                name, deque(maxlen=max(self.config.vol_window, 2))
            )
        values: list[float] = list(series)
        returns: list[float] = []
        index: int
        for index in range(1, len(values)):
            previous: float = values[index - 1]
            if previous == 0:
                continue
            returns.append(values[index] / previous - 1.0)
        return returns

    def volatility(self, name: str) -> float:
        """Return the annualization-free sample volatility of returns."""
        returns: list[float] = self.equity_returns(name)
        if len(returns) < 2:
            return 0.0
        value: float = statistics.pstdev(returns)
        if math.isnan(value):
            return 0.0
        return value

    def allocate(
        self, total_equity: float, strategies: list[str], vols: dict[str, float] | None = None
    ) -> dict[str, float]:
        """Return the target capital per strategy."""
        if total_equity <= 0 or not strategies:
            return {}

        vol_map: dict[str, float] = vols or {}
        weights: dict[str, float]
        if self.config.method == "inverse_volatility":
            weights = self._inverse_volatility_weights(strategies, vol_map)
        else:
            weights = self._equal_weights(strategies)

        weights = self._apply_bounds(weights)

        usable: float = total_equity * (1.0 - self.config.cash_fraction)
        allocation: dict[str, float] = {}
        name: str
        for name, weight in weights.items():
            allocation[name] = round(usable * weight, 2)
        return allocation

    def _equal_weights(self, strategies: list[str]) -> dict[str, float]:
        """Identical weights across strategies."""
        count: int = len(strategies)
        weight: float = 1.0 / count
        return {name: weight for name in strategies}

    def _inverse_volatility_weights(
        self, strategies: list[str], vol_map: dict[str, float]
    ) -> dict[str, float]:
        """Weights proportional to inverse volatility."""
        inverse: dict[str, float] = {}
        name: str
        for name in strategies:
            vol: float | None = vol_map.get(name)
            if vol is None:
                vol = self.volatility(name)
            if vol <= 0:
                inverse[name] = 1.0
            else:
                inverse[name] = 1.0 / vol

        total: float = sum(inverse.values())
        if total <= 0:
            return self._equal_weights(strategies)
        return {name: value / total for name, value in inverse.items()}

    def _apply_bounds(self, weights: dict[str, float]) -> dict[str, float]:
        """Clamp weights into [min_weight, max_weight].

        Uses iterative water-filling: names above the cap are pinned to the
        cap and the remaining mass is redistributed among the rest. When
        len(strategies) * max_weight < 1 the caps cannot hold the whole
        mass, so the remainder stays in cash instead of being renormalized
        back above the limit.
        """
        if not weights:
            return {}

        min_weight: float = max(self.config.min_weight, 0.0)
        max_weight: float = (
            min(self.config.max_weight, 1.0) if self.config.max_weight > 0 else 1.0
        )

        if max_weight * len(weights) < 1.0 - 1e-9:
            bounded: dict[str, float] = {
                name: min(max(weight, min_weight), max_weight)
                for name, weight in weights.items()
            }
            return bounded

        # Iterative water-filling against the per-strategy cap.
        fixed: dict[str, float] = {}
        for _ in range(len(weights) + 1):
            free_names: list[str] = [n for n in weights if n not in fixed]
            if not free_names:
                break

            remaining: float = 1.0 - sum(fixed.values())
            if remaining <= 0:
                break

            free_total: float = sum(max(weights[name], 0.0) for name in free_names)
            if free_total <= 0:
                share: float = remaining / len(free_names)
                for name in free_names:
                    fixed[name] = share
                break

            scaled: dict[str, float] = {
                name: max(weights[name], 0.0) / free_total * remaining
                for name in free_names
            }
            over: list[str] = [
                name for name, value in scaled.items() if value > max_weight + 1e-12
            ]
            if not over:
                for name, value in scaled.items():
                    fixed[name] = max(min(value, max_weight), min_weight)
                break

            for name in over:
                fixed[name] = max_weight

        fixed_total: float = sum(fixed.values())
        if fixed_total <= 0:
            return self._equal_weights(list(weights.keys()))
        return {name: value / fixed_total for name, value in fixed.items()}

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return a JSON-serializable snapshot of tracked equity."""
        with self._lock:
            result: dict[str, dict[str, object]] = {}
            name: str
            for name, series in self._equity.items():
                values: list[float] = list(series)
                result[name] = {
                    "samples": len(values),
                    "last_equity": values[-1] if values else None,
                    "volatility": round(self.volatility(name), 6),
                }
            return result
