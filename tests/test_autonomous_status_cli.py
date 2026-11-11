"""Tests for the runtime status CLI subcommand.

The status command is the operator's window into an unattended process: it
must report a missing state file honestly, parse a healthy state payload into
a one-line summary, and survive a malformed file without crashing.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path




from vnpy.autonomous.__main__ import _format_state_line, _read_state_file


REPO_ROOT: Path = Path(__file__).resolve().parents[1]


TEMPLATE_CONFIG: Path = (
    REPO_ROOT / "examples" / "autonomous" / "autonomous.json"
)


def make_state_file(tmp_path: Path, age_seconds: int = 30) -> Path:
    """Write a representative state payload into a temp .vntrader folder."""
    config_dir: Path = tmp_path / ".vntrader"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path: Path = config_dir / "autonomous.json"
    config_path.write_text(TEMPLATE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    updated_at: str = (
        datetime.now() - timedelta(seconds=age_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    payload: dict[str, object] = {
        "pid": 12345,
        "parent": True,
        "mode": "paper",
        "status": "supervising",
        "session": "day",
        "session_day": "2026-11-12",
        "calendar_profile": "cn_futures",
        "started_at": updated_at,
        "updated_at": updated_at,
        "uptime_secs": 42.1,
        "next_open": "2026-11-12T13:30:00",
        "restarts_in_session": 0,
        "guard": {"armed": False},
        "health": {"halted": False},
        "portfolio": {},
        "ledger": {},
        "strategy_engines": 1,
        "gateway_last_event": {},
    }
    state_path: Path = config_dir / "autonomous_state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def test_read_state_file_resolves_next_to_config(tmp_path: Path) -> None:
    config_path: Path = make_state_file(tmp_path)
    state_path: Path = _read_state_file(str(config_path))
    assert state_path.exists()
    assert state_path.name == "autonomous_state.json"


def test_format_state_line_reports_fresh_heartbeat() -> None:
    import json as _json

    payload: dict = _json.loads(
        '{"pid": 1, "mode": "paper", "status": "supervising", "session": "day",'
        ' "uptime_secs": 12.5, "restarts_in_session": 0, "strategy_engines": 2,'
        ' "guard": {"armed": false}, "health": {"halted": false},'
        f' "updated_at": "{datetime.now().isoformat()}"}}'
    )
    line: str = _format_state_line(payload)
    assert "status=supervising" in line
    assert "heartbeat_age=0s" in line
    assert "guard_armed=False" in line
    assert "guard_breached=False" in line
    assert "health_halted=False" in line


def test_format_state_line_survives_bad_timestamp() -> None:
    payload: dict = {
        "pid": 1,
        "mode": "paper",
        "status": "starting",
        "session": None,
        "uptime_secs": 0.0,
        "restarts_in_session": 0,
        "strategy_engines": 0,
        "guard": {"armed": True},
        "health": {"halted": True},
        "updated_at": "not-a-timestamp",
    }
    line: str = _format_state_line(payload)
    assert "heartbeat_age=unknown" in line
    assert "guard_armed=True" in line
    assert "health_halted=True" in line


def test_status_cli_reports_missing_state_file(tmp_path: Path) -> None:
    config_dir: Path = tmp_path / ".vntrader"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path: Path = config_dir / "autonomous.json"
    config_path.write_text(TEMPLATE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "vnpy.autonomous", "status", "--config", str(config_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert "状态文件不存在" in combined
    assert result.returncode == 2


def test_status_cli_reports_live_payload(tmp_path: Path) -> None:
    config_path: Path = make_state_file(tmp_path)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "vnpy.autonomous", "status", "--config", str(config_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert "pid=12345" in combined
    assert "status=supervising" in combined
    assert "heartbeat_age=" in combined
    assert result.returncode == 0
