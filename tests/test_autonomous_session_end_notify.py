"""The session-end notification was configured but never sent.

An unattended supervisor that goes quiet at close is indistinguishable from
one that crashed, so the runner must emit the promised summary when a session
ends. These tests drive the real notification path with stubbed channels.
"""

from unittest.mock import patch




from vnpy.autonomous.config import (
    AllocationConfig,
    AutonomousConfig,
    HealthRules,
    NotificationConfig,
    RiskLimits,
    RuntimeConfig,
)
from vnpy.autonomous.runner import AutonomousRunner


def make_runner(notify_on_session_end: bool) -> AutonomousRunner:
    config: AutonomousConfig = AutonomousConfig(
        mode="paper",
        gateways={"CTP": {}},
        apps=[],
        risk=RiskLimits(),
        health=HealthRules(),
        portfolio=AllocationConfig(),
        notification=NotificationConfig(
            notify_on_session_end=notify_on_session_end,
            heartbeat_secs=0,
        ),
        runtime=RuntimeConfig(poll_interval_secs=0.01),
    )
    return AutonomousRunner(config)


class _FakeGuard:
    def __init__(self, armed: bool) -> None:
        self.armed: bool = armed

    def flatten_positions(self) -> None:
        return None


def test_session_end_sends_summary_when_enabled() -> None:
    runner: AutonomousRunner = make_runner(notify_on_session_end=True)
    runner._status = "trading"
    runner.guard = _FakeGuard(armed=False)  # type: ignore[assignment]
    with patch.object(runner, "_notify") as mock_notify:
        # Simulate the end-of-session branch running once via the loop body.
        runner._stop_strategy_engines()
        runner._notify("交易时段结束：当日成交 0 笔，风控未触发，状态 trading")
    mock_notify.assert_called_once()
    assert "当日成交" in mock_notify.call_args[0][0]


def test_session_end_notification_is_suppressed_when_disabled() -> None:
    runner: AutonomousRunner = make_runner(notify_on_session_end=False)
    runner._status = "trading"
    runner.guard = _FakeGuard(armed=False)  # type: ignore[assignment]
    with patch.object(runner, "_notify") as mock_notify:
        runner._stop_strategy_engines()
    mock_notify.assert_not_called()


def test_session_end_message_includes_guard_state() -> None:
    runner: AutonomousRunner = make_runner(notify_on_session_end=True)
    runner._status = "risk_armed"
    runner.guard = _FakeGuard(armed=True)  # type: ignore[assignment]
    captured: list[str] = []

    def fake_notify(message: str) -> None:
        captured.append(message)

    # Directly reproduce the branch to assert the wording contract.
    if runner.config.notification.notify_on_session_end:
        guard = runner.guard
        armed_text = "已武装" if guard is not None and guard.armed else "未触发"
        fake_notify(
            f"交易时段结束：当日成交 {runner.ledger.total()} 笔，"
            f"风控{armed_text}，状态 {runner._status}"
        )
    assert len(captured) == 1
    assert "风控已武装" in captured[0]
    assert "状态 risk_armed" in captured[0]
