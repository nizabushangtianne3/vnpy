"""Autonomous trading operations layer for VeighNa.

This package adds session-aware supervision, pre-trade risk control,
strategy health circuit breakers and capital allocation on top of the
core trading engine, so a portfolio can run unattended and survive
restarts, holidays and feed outages.
"""

from .calendar import (
    CalendarProfile,
    Session,
    TradingCalendar,
    build_profile,
    load_holidays,
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
from .adapters.cta import CtaStatsAdapter
from .health import HealthMonitor, StrategyHealth
from .portfolio import CapitalAllocator
from .risk import RiskGuard, RiskState
from .runner import (
    APP_REGISTRY,
    GATEWAY_REGISTRY,
    AutonomousRunner,
    TradeLedger,
    child_entry,
    read_state,
)


__all__ = [
    "APP_REGISTRY",
    "AllocationConfig",
    "AutonomousConfig",
    "AutonomousRunner",
    "CalendarProfile",
    "CapitalAllocator",
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
    "load_config",
    "load_holidays",
    "read_state",
    "save_config",
    "validate_config",
]
