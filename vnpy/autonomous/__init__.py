"""Autonomous trading operations layer for VeighNa.

This package adds session-aware supervision, pre-trade risk control,
strategy health circuit breakers and capital allocation on top of the
core trading engine, so a portfolio can run unattended and survive
restarts, holidays and feed outages.
"""

from .calendar import (
    CN_FUTURES_HOLIDAYS_2026,
    CalendarProfile,
    Session,
    TradingCalendar,
    build_profile,
    load_holidays,
    merge_holidays,
)
from .config import (
    AllocationConfig,
    AutonomousConfig,
    HealthRules,
    NotificationConfig,
    RiskLimits,
    RuntimeConfig,
    load_config,
    save_config,
    validate_config,
)
from .event import EVENT_STRATEGY_SNAPSHOT, StrategySnapshot
try:
    from .backtest import BacktestResult, BacktestSettings, run_backtest

    _HAS_BACKTEST: bool = True
except ImportError:  # pragma: no cover - vnpy_ctastrategy is optional
    _HAS_BACKTEST = False

from .adapters.cta import CtaStatsAdapter
from .health import HealthMonitor, StrategyHealth
from .portfolio import CapitalAllocator
from .risk import RiskGuard, RiskState
from .runner import (
    APP_REGISTRY,
    GATEWAY_REGISTRY,
    AutonomousRunner,
    TradeLedger,
    atomic_write_json,
    child_entry,
    ensure_utf8_output,
    read_state,
)


__all__ = [
    "BacktestResult",
    "BacktestSettings",
    "run_backtest",
    "APP_REGISTRY",
    "AllocationConfig",
    "AutonomousConfig",
    "AutonomousRunner",
    "CalendarProfile",
    "CapitalAllocator",
    "CN_FUTURES_HOLIDAYS_2026",
    "CtaStatsAdapter",
    "EVENT_STRATEGY_SNAPSHOT",
    "GATEWAY_REGISTRY",
    "HealthMonitor",
    "HealthRules",
    "NotificationConfig",
    "RiskGuard",
    "RiskLimits",
    "RiskState",
    "RuntimeConfig",
    "Session",
    "StrategyHealth",
    "StrategySnapshot",
    "TradeLedger",
    "TradingCalendar",
    "build_profile",
    "child_entry",
    "ensure_utf8_output",
    "load_config",
    "load_holidays",
    "merge_holidays",
    "read_state",
    "save_config",
    "atomic_write_json",
    "validate_config",
]
