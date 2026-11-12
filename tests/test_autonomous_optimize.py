"""Grid parsing and ranking for the parameter optimization CLI.

A parameter scan is only useful if the expansion is deterministic and the
ranking is honest: the parser must reject malformed input instead of silently
running one configuration, and the output table must order by Sharpe.
"""

import subprocess
import sys
from pathlib import Path
import json


import pytest


from vnpy.autonomous.__main__ import _parse_grid


REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def test_parse_grid_expands_cartesian_product() -> None:
    grid: list[dict[str, int | float]] = _parse_grid(
        "fast_window=5,10;slow_window=20,40"
    )
    assert grid == [
        {"fast_window": 5, "slow_window": 20},
        {"fast_window": 5, "slow_window": 40},
        {"fast_window": 10, "slow_window": 20},
        {"fast_window": 10, "slow_window": 40},
    ]


def test_parse_grid_accepts_floats() -> None:
    grid: list[dict[str, int | float]] = _parse_grid("stop_atr=1.5,2.0")
    assert grid == [{"stop_atr": 1.5}, {"stop_atr": 2.0}]


def test_parse_grid_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match="等号"):
        _parse_grid("fast_window=5;slow_window")


def test_parse_grid_rejects_empty_value_list() -> None:
    with pytest.raises(ValueError, match="可用取值"):
        _parse_grid("fast_window=")


def test_optimize_cli_writes_json_output(tmp_path: Path) -> None:
    """The scan is only useful if the machine can consume its ranking."""
    import os

    output_file = tmp_path / "optimize_result.json"
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "vnpy.autonomous",
            "optimize",
            "--grid",
            "fast_window=5,10;slow_window=20",
            "--bars",
            "120",
            "--output",
            str(output_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert f"结果已写入：{output_file}" in combined, combined
    assert output_file.is_file(), combined
    payload: dict = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["data_bars"] == 120
    results: list[dict] = payload["results"]
    assert len(results) == 2
    assert {"total_pnl", "sharpe", "params"} <= set(results[0])
    assert payload["best"]["params"] == results[0]["params"]


def test_optimize_cli_writes_walk_forward_output(tmp_path: Path) -> None:
    """Walk-forward output must include the verdict inputs for automation."""
    import os

    output_file = tmp_path / "walk_forward_result.json"
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "vnpy.autonomous",
            "optimize",
            "--grid",
            "fast_window=5,10;slow_window=20,40",
            "--bars",
            "400",
            "--train-bars",
            "150",
            "--test-bars",
            "50",
            "--output",
            str(output_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert f"结果已写入：{output_file}" in combined, combined
    assert output_file.is_file(), combined
    payload: dict = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["train_bars"] == 150
    assert payload["test_bars"] == 50
    folds: list[dict] = payload["folds"]
    assert folds
    assert {"fold", "best_params", "test_pnl", "test_sharpe"} <= set(folds[0])


def test_optimize_cli_runs_and_prints_ranking() -> None:
    """Run the CLI through the same interpreter pytest is using."""
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "vnpy.autonomous",
            "optimize",
            "--grid",
            "fast_window=5,10;slow_window=20",
            "--bars",
            "120",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=180,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert "参数组合数：2" in combined, combined
    assert "params" in combined
    assert "fast_window=5,slow_window=20" in combined
    # The CLI is free to return 1 when even the best parameter combination has
    # no edge on the synthetic series; the test only checks that the ranking
    # ran and reported something meaningful.
    assert result.returncode in (0, 1)

def test_optimize_walk_forward_runs_and_reports_overfit() -> None:
    """Walk-forward on a synthetic series: report folds and honest verdicts."""
    import os
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "vnpy.autonomous",
            "optimize",
            "--grid",
            "fast_window=5,10;slow_window=20,40",
            "--bars",
            "400",
            "--train-bars",
            "150",
            "--test-bars",
            "50",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert "walk-forward 验证：训练 150 根" in combined, combined
    assert "样本内合计 pnl=" in combined
    assert "样本外合计 pnl=" in combined
    assert "结论：" in combined
    # The verdict here is data dependent, but the CLI must exit with 0 or 1.
    assert result.returncode in (0, 1)


def test_optimize_rejects_mismatched_walk_forward_lengths() -> None:
    import os
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "vnpy.autonomous",
            "optimize",
            "--grid",
            "fast_window=5",
            "--bars",
            "120",
            "--train-bars",
            "100",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert "--train-bars 与 --test-bars 必须同时设置" in combined
    assert result.returncode == 2


def test_optimize_rejects_bars_below_single_fold() -> None:
    """Fewer bars than train+test must fail cleanly, not crash on /0."""
    import os
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "vnpy.autonomous",
            "optimize",
            "--grid",
            "fast_window=5",
            "--bars",
            "120",
            "--train-bars",
            "100",
            "--test-bars",
            "25",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    combined: str = result.stdout + result.stderr
    assert "K线不足以组成一个完整的 walk-forward 折叠" in combined, combined
    assert result.returncode == 2
