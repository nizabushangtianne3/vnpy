"""Custom event types and payloads for autonomous trading."""

from __future__ import annotations

from dataclasses import dataclass


EVENT_STRATEGY_SNAPSHOT: str = "eStrategySnapshot"


@dataclass
class StrategySnapshot:
    """Per-strategy statistics published to the health monitor.

    ``realized_pnl`` and ``trades`` are optional: providers that cannot
    attribute profit to a strategy may omit them, and pnl-based health
    rules stay inactive for those strategies.
    """

    strategy_name: str
    trading: bool = False
    positions: int = 0
    trades: int | None = None
    realized_pnl: float | None = None
    equity: float | None = None
    error: str | None = None
