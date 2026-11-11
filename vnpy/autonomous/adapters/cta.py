"""Publish vnpy_ctastrategy engine stats as strategy snapshot events.

The adapter only relies on duck-typed attributes of a CTA-like engine, so it
also works with test doubles and with engine subclasses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from vnpy.event import Event, EventEngine

from ..event import EVENT_STRATEGY_SNAPSHOT, StrategySnapshot


class CtaStatsAdapter:
    """Translate ``engine.strategies`` state into StrategySnapshot events.

    ``realized_pnl`` and ``equity`` are read from the strategy ``variables``
    dict when present (strategies are expected to publish them through
    ``sync_variables``), otherwise the snapshot carries trade counts and
    trading state only.
    """

    def __init__(
        self,
        engine: Any,
        event_engine: EventEngine,
        clock: Any = None,
    ) -> None:
        """Bind the adapter to a CTA-like strategy engine."""
        self.engine: Any = engine
        self.event_engine: EventEngine = event_engine
        self._clock: Any = clock or datetime.now

    def collect_snapshots(self) -> list[StrategySnapshot]:
        """Build snapshots from the current engine strategy state."""
        strategies: Any = getattr(self.engine, "strategies", None)
        if not isinstance(strategies, dict):
            return []

        snapshots: list[StrategySnapshot] = []
        name: str
        strategy: Any
        for name, strategy in strategies.items():
            snapshots.append(self._build_snapshot(str(name), strategy))
        return snapshots

    def publish_snapshots(self) -> list[StrategySnapshot]:
        """Publish snapshots to the event engine and return them."""
        snapshots: list[StrategySnapshot] = self.collect_snapshots()
        snapshot: StrategySnapshot
        for snapshot in snapshots:
            self.event_engine.put(Event(EVENT_STRATEGY_SNAPSHOT, snapshot))
        return snapshots

    def _build_snapshot(self, name: str, strategy: Any) -> StrategySnapshot:
        """Build one StrategySnapshot from a strategy object."""
        trading: bool = bool(getattr(strategy, "trading", False))
        inited: bool = bool(getattr(strategy, "inited", False))

        position_value: Any = getattr(strategy, "pos", 0)
        positions: int = 1 if abs(_to_float(position_value)) > 0 else 0

        trades: int | None = None
        trades_value: Any = getattr(strategy, "trades", None)
        if isinstance(trades_value, int):
            trades = trades_value
        elif isinstance(trades_value, list):
            trades = len(trades_value)

        variables: Any = getattr(strategy, "variables", None)
        realized_pnl: float | None = None
        equity: float | None = None
        if isinstance(variables, dict):
            realized_pnl = _to_optional_float(variables.get("realized_pnl"))
            equity = _to_optional_float(variables.get("equity"))

        return StrategySnapshot(
            strategy_name=name,
            trading=trading and inited,
            positions=positions,
            trades=trades,
            realized_pnl=realized_pnl,
            equity=equity,
        )


def _to_float(value: Any) -> float:
    """Best-effort float conversion returning 0.0 on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_optional_float(value: Any) -> float | None:
    """Best-effort float conversion returning None on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
