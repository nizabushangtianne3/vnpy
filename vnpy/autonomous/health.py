"""Strategy health monitoring with circuit breaker rules."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import HealthRules


@dataclass
class StrategyHealth:
    """Running statistics for one strategy."""

    name: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_reset_date: date | None = None
    consecutive_losses: int = 0
    error_times: deque[datetime] = field(default_factory=deque)
    error_count: int = 0
    halted: bool = False
    halt_reason: str = ""
    halted_at: datetime | None = None
    last_event_time: datetime | None = None
    equity_samples: deque[tuple[datetime, float]] = field(
        default_factory=lambda: deque(maxlen=1000)
    )


class HealthMonitor:
    """Track per-strategy health and apply circuit breaker rules."""

    def __init__(
        self,
        rules: HealthRules,
        clock: Callable[[], datetime] | None = None,
        pause_callback: Callable[[str, str], None] | None = None,
        resume_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Create a monitor with the given rules and optional callbacks."""
        self.rules: HealthRules = rules
        self._clock: Callable[[], datetime] = clock or datetime.now
        self._pause_callback: Callable[[str, str], None] | None = pause_callback
        self._resume_callback: Callable[[str], None] | None = resume_callback
        self._strategies: dict[str, StrategyHealth] = {}
        self._lock: threading.RLock = threading.RLock()

    def set_pause_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set the callback invoked when a strategy must be paused."""
        self._pause_callback = callback

    def set_resume_callback(self, callback: Callable[[str], None]) -> None:
        """Set the callback invoked when a strategy may resume."""
        self._resume_callback = callback

    # ---------------------------------------------------------------- feed

    def register_strategy(self, name: str) -> None:
        """Start tracking a strategy under the given name."""
        with self._lock:
            if name not in self._strategies:
                self._strategies[name] = StrategyHealth(name=name)

    def record_trade(self, name: str, pnl: float, when: datetime | None = None) -> None:
        """Record one realized trade for a strategy."""
        now: datetime = when or self._clock()
        with self._lock:
            health: StrategyHealth = self._strategies.setdefault(
                name, StrategyHealth(name=name)
            )
            self._roll_daily(health, now)
            health.trades += 1
            health.realized_pnl += pnl
            health.daily_pnl += pnl
            health.last_event_time = now
            if pnl > 0:
                health.wins += 1
                health.consecutive_losses = 0
            elif pnl < 0:
                health.losses += 1
                health.consecutive_losses += 1

            self._evaluate(health, now)

    def record_error(self, name: str, when: datetime | None = None) -> None:
        """Record one error occurrence for a strategy."""
        now: datetime = when or self._clock()
        with self._lock:
            health: StrategyHealth = self._strategies.setdefault(
                name, StrategyHealth(name=name)
            )
            window: timedelta = timedelta(seconds=self.rules.error_window_secs)
            health.error_times.append(now)
            while health.error_times and now - health.error_times[0] > window:
                health.error_times.popleft()
            health.error_count = len(health.error_times)
            health.last_event_time = now

            self._evaluate(health, now)

    def record_equity(self, name: str, equity: float, when: datetime | None = None) -> None:
        """Record an equity sample used for volatility estimation."""
        now: datetime = when or self._clock()
        with self._lock:
            health: StrategyHealth = self._strategies.setdefault(
                name, StrategyHealth(name=name)
            )
            health.equity_samples.append((now, equity))

    def halt(self, name: str, reason: str, when: datetime | None = None) -> None:
        """Externally halt a strategy."""
        now: datetime = when or self._clock()
        with self._lock:
            health: StrategyHealth = self._strategies.setdefault(
                name, StrategyHealth(name=name)
            )
            if health.halted:
                return
            health.halted = True
            health.halt_reason = reason
            health.halted_at = now

        callback: Callable[[str, str], None] | None = self._pause_callback
        if callback is not None:
            callback(name, reason)

    def resume(self, name: str, force: bool = False, when: datetime | None = None) -> bool:
        """Resume a halted strategy after the cooldown period."""
        now: datetime = when or self._clock()
        with self._lock:
            health: StrategyHealth | None = self._strategies.get(name)
            if health is None or not health.halted:
                return False
            halted_at: datetime | None = health.halted_at
            if halted_at is not None and not force:
                cooldown: timedelta = timedelta(seconds=self.rules.halt_cooldown_secs)
                if now - halted_at < cooldown:
                    return False
            health.halted = False
            health.halt_reason = ""
            health.halted_at = None

        callback: Callable[[str], None] | None = self._resume_callback
        if callback is not None:
            callback(name)
        return True

    def resume_all_due(self, when: datetime | None = None) -> list[str]:
        """Resume every strategy whose cooldown has elapsed."""
        now: datetime = when or self._clock()
        resumed: list[str] = []
        with self._lock:
            names: list[str] = list(self._strategies.keys())
        for name in names:
            if self.resume(name, when=now):
                resumed.append(name)
        return resumed

    # --------------------------------------------------------------- query

    def is_halted(self, name: str) -> bool:
        """Whether the strategy is currently halted."""
        with self._lock:
            health: StrategyHealth | None = self._strategies.get(name)
            return health.halted if health is not None else False

    def get_health(self, name: str) -> StrategyHealth | None:
        """Return the health snapshot of one strategy."""
        with self._lock:
            return self._strategies.get(name)

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return a JSON-serializable snapshot of all strategies."""
        with self._lock:
            result: dict[str, dict[str, object]] = {}
            name: str
            health: StrategyHealth
            for name, health in self._strategies.items():
                result[name] = {
                    "trades": health.trades,
                    "wins": health.wins,
                    "losses": health.losses,
                    "realized_pnl": health.realized_pnl,
                    "daily_pnl": health.daily_pnl,
                    "consecutive_losses": health.consecutive_losses,
                    "error_count": health.error_count,
                    "halted": health.halted,
                    "halt_reason": health.halt_reason,
                    "halted_at": (
                        health.halted_at.isoformat() if health.halted_at else None
                    ),
                    "win_rate": (
                        health.wins / health.trades if health.trades else 0.0
                    ),
                }
            return result

    def restore(
        self,
        payload: dict[str, dict[str, object]],
        session_day: str | None = None,
    ) -> None:
        """Restore persisted health counters, e.g. after a child restart.

        Only accepted when the payload was written today (``session_day``
        matches the clock date): yesterday's halt state and daily counters
        must not leak into today's decisions. The restore happens before
        strategies are started, so a strategy that was halted when the
        previous process died stays paused.
        """
        if not session_day or self._clock().date().isoformat() != session_day:
            return

        with self._lock:
            for name, data in payload.items():
                if not isinstance(name, str) or not isinstance(data, dict):
                    continue
                halted_at_raw: object = data.get("halted_at")
                halted_at: datetime | None = None
                if isinstance(halted_at_raw, str):
                    try:
                        halted_at = datetime.fromisoformat(halted_at_raw)
                    except ValueError:
                        continue
                elif halted_at_raw is not None:
                    continue

                health: StrategyHealth = self._strategies.setdefault(
                    name, StrategyHealth(name=name)
                )
                trades_obj: object = data.get("trades")
                health.trades = int(trades_obj) if isinstance(trades_obj, int) else 0
                wins_obj: object = data.get("wins")
                health.wins = int(wins_obj) if isinstance(wins_obj, int) else 0
                losses_obj: object = data.get("losses")
                health.losses = int(losses_obj) if isinstance(losses_obj, int) else 0
                realized = data.get("realized_pnl")
                if isinstance(realized, (int, float)):
                    health.realized_pnl = float(realized)
                daily = data.get("daily_pnl")
                if isinstance(daily, (int, float)):
                    health.daily_pnl = float(daily)
                health.daily_reset_date = self._clock().date()
                consecutive_obj: object = data.get("consecutive_losses")
                health.consecutive_losses = (
                    int(consecutive_obj)
                    if isinstance(consecutive_obj, int)
                    else 0
                )
                error_obj: object = data.get("error_count")
                health.error_count = (
                    int(error_obj) if isinstance(error_obj, int) else 0
                )
                if halted_at is not None:
                    health.halted = True
                    health.halt_reason = str(data.get("halt_reason", ""))
                    health.halted_at = halted_at

        callback: Callable[[str, str], None] | None = self._pause_callback
        if callback is not None:
            for name, data in payload.items():
                if isinstance(name, str) and data.get("halted"):
                    callback(name, str(data.get("halt_reason", "")))

    # ------------------------------------------------------------ internal

    def _roll_daily(self, health: StrategyHealth, now: datetime) -> None:
        """Reset daily counters when the calendar day changes."""
        current_day: date = now.date()
        if health.daily_reset_date != current_day:
            health.daily_reset_date = current_day
            health.daily_pnl = 0.0

    def _evaluate(self, health: StrategyHealth, now: datetime) -> None:
        """Apply circuit breaker rules to one strategy."""
        if health.halted:
            return

        rules: HealthRules = self.rules

        if rules.max_consecutive_losses > 0:
            if health.consecutive_losses >= rules.max_consecutive_losses:
                self._halt(
                    health,
                    f"连续亏损{health.consecutive_losses}次，达到上限{rules.max_consecutive_losses}",
                    now,
                )
                return

        if rules.max_daily_loss > 0:
            if health.daily_pnl <= -rules.max_daily_loss:
                self._halt(
                    health,
                    f"当日亏损{health.daily_pnl:.2f}，达到上限{rules.max_daily_loss:.2f}",
                    now,
                )
                return

        if rules.max_error_count > 0:
            if health.error_count >= rules.max_error_count:
                self._halt(
                    health,
                    f"错误次数{health.error_count}，达到上限{rules.max_error_count}",
                    now,
                )

    def _halt(self, health: StrategyHealth, reason: str, now: datetime) -> None:
        """Halt a strategy and invoke the pause callback."""
        health.halted = True
        health.halt_reason = reason
        health.halted_at = now

        callback: Callable[[str, str], None] | None = self._pause_callback
        if callback is not None:
            callback(health.name, reason)
