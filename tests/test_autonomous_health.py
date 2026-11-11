from datetime import datetime, timedelta

from vnpy.autonomous.config import HealthRules
from vnpy.autonomous.health import HealthMonitor


def fixed_clock(start: datetime):
    state = {"now": start}

    def now() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


def test_consecutive_losses_halt_strategy() -> None:
    pauses: list[tuple[str, str]] = []
    monitor = HealthMonitor(
        HealthRules(max_consecutive_losses=3, halt_cooldown_secs=0)
    )
    monitor.set_pause_callback(lambda name, reason: pauses.append((name, reason)))

    base = datetime(2026, 10, 9, 9, 30)
    monitor.record_trade("trend", -100, base)
    monitor.record_trade("trend", -50, base)
    assert monitor.is_halted("trend") is False

    monitor.record_trade("trend", -20, base)
    assert monitor.is_halted("trend") is True
    assert pauses and pauses[0][0] == "trend"

    monitor.record_trade("trend", 500, base)
    assert monitor.get_health("trend").realized_pnl == 330


def test_winning_trade_resets_consecutive_losses() -> None:
    monitor = HealthMonitor(HealthRules(max_consecutive_losses=2, halt_cooldown_secs=0))
    base = datetime(2026, 10, 9, 9, 30)

    monitor.record_trade("trend", -10, base)
    monitor.record_trade("trend", 5, base)
    monitor.record_trade("trend", -10, base)
    assert monitor.is_halted("trend") is False

    monitor.record_trade("trend", -11, base)
    assert monitor.is_halted("trend") is True


def test_daily_loss_rule_and_daily_reset() -> None:
    monitor = HealthMonitor(HealthRules(max_daily_loss=1000, max_consecutive_losses=0))
    base = datetime(2026, 10, 9, 9, 30)

    monitor.record_trade("trend", -600, base)
    monitor.record_trade("trend", -500, base)
    assert monitor.is_halted("trend") is True

    later = datetime(2026, 10, 10, 9, 30)
    health = monitor.get_health("trend")
    assert health is not None
    monitor.record_trade("trend", 0, later)
    assert monitor.get_health("trend").daily_pnl == 0


def test_error_window_halts_strategy() -> None:
    monitor = HealthMonitor(
        HealthRules(max_error_count=3, error_window_secs=60, halt_cooldown_secs=0)
    )
    base = datetime(2026, 10, 9, 9, 30)

    monitor.record_error("trend", base)
    monitor.record_error("trend", base + timedelta(seconds=30))
    assert monitor.is_halted("trend") is False

    monitor.record_error("trend", base + timedelta(seconds=40))
    assert monitor.is_halted("trend") is True


def test_cooldown_resume_rules() -> None:
    now, advance = fixed_clock(datetime(2026, 10, 9, 21, 0))
    resumes: list[str] = []
    monitor = HealthMonitor(
        HealthRules(max_consecutive_losses=1, halt_cooldown_secs=1800), clock=now
    )
    monitor.set_resume_callback(lambda name: resumes.append(name))

    monitor.record_trade("trend", -10)
    assert monitor.is_halted("trend") is True

    assert monitor.resume("trend") is False
    advance(1801)
    assert monitor.resume("trend") is True
    assert monitor.is_halted("trend") is False
    assert resumes == ["trend"]


def test_resume_all_due_and_snapshot() -> None:
    now, advance = fixed_clock(datetime(2026, 10, 9, 21, 0))
    monitor = HealthMonitor(
        HealthRules(max_consecutive_losses=1, halt_cooldown_secs=60), clock=now
    )

    monitor.record_trade("trend", -10)
    monitor.record_trade("momentum", -20)
    assert monitor.resume_all_due() == []

    advance(61)
    assert monitor.resume_all_due() == ["trend", "momentum"]

    snapshot = monitor.snapshot()
    assert snapshot["trend"]["trades"] == 1
    assert snapshot["trend"]["halted"] is False
    assert snapshot["momentum"]["losses"] == 1
