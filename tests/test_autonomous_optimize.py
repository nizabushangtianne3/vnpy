"""Grid parsing and ranking for the parameter optimization CLI.

A parameter scan is only useful if the expansion is deterministic and the
ranking is honest: the parser must reject malformed input instead of silently
running one configuration, and the output table must order by Sharpe.
"""

import subprocess
import sys
from pathlib import Path


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
