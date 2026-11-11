"""Integration tests for the parent supervisor layer.

The first test spawns a real child process through multiprocessing, so the
whole parent chain (spawn, config reload, state handoff, terminate) runs
for real, not through mocks.
"""

from datetime import datetime, timedelta
import threading
import time
from types import SimpleNamespace

import pytest

from vnpy.autonomous.config import AutonomousConfig, RuntimeConfig, save_config
from vnpy.autonomous.runner import AutonomousRunner, read_state


def build_parent_config(tmp_path, apps: list[str]) -> str:
    config = AutonomousConfig(
        mode="paper",
        gateways={"CTP": {}},
        apps=apps,
        calendar={"profile": "full_day"},
        runtime=RuntimeConfig(
            poll_interval_secs=0.2,
            contract_wait_secs=0,
            strategy_init_wait_secs=0,
            state_file="parent_state.json",
            watchdog_stall_secs=3600,
            max_restarts_per_session=3,
            restart_backoff_secs=1,
        ),
    )
    path = tmp_path / "autonomous.json"
    save_config(config, path)
    return str(path)


def wait_for(predicate, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def test_restart_limit_recovers_after_cooldown(tmp_path) -> None:
    runner = AutonomousRunner.from_config_file(
        build_parent_config(tmp_path, apps=[])
    )
    runner.stop_event = threading.Event()
    runtime = SimpleNamespace(max_restarts_per_session=2, restart_backoff_secs=0.05)

    runner.restarts_in_session = 2
    runner._handle_restart_limit(runtime)
    assert runner.restarts_in_session == 2
    assert runner._restart_limit_at is not None

    # 冷却未到，保持等待
    runner._handle_restart_limit(runtime)
    assert runner.restarts_in_session == 2

    # 冷却下限为 30 秒，回拨时间戳模拟冷却结束
    runner._restart_limit_at = datetime.now() - timedelta(seconds=31)
    runner._handle_restart_limit(runtime)
    assert runner.restarts_in_session == 0
    assert runner._restart_limit_at is None


def test_parent_spawns_and_stops_real_child(tmp_path) -> None:
    try:
        import vnpy_ctastrategy  # noqa: F401

        apps = ["CtaStrategy"]
    except ImportError:
        pytest.skip("vnpy_ctastrategy 未安装，跳过父进程真实验程")

    config_path = build_parent_config(tmp_path, apps=apps)
    state_path = tmp_path / "parent_state.json"

    stop_event = threading.Event()
    runner = AutonomousRunner.from_config_file(config_path)
    runner.stop_event = stop_event

    thread = threading.Thread(target=runner.run_parent, daemon=True)
    thread.start()
    try:
        def child_ready():
            payload = read_state(state_path)
            if (
                payload.get("parent") is False
                and payload.get("status") in ("strategies_started", "trading")
            ):
                return payload
            return None

        # 父进程先写 supervising/child_running，子进程随后写自己的状态
        payload = wait_for(child_ready)
        assert payload is not None, "子进程状态未在超时内出现"
        assert payload["session"] == "all_day"
        assert payload["strategy_engines"] >= 1
    finally:
        stop_event.set()
        thread.join(timeout=60)

    assert not thread.is_alive()
