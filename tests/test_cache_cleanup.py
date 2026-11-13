"""The cleanup must delete what it consumed and nothing else."""

import subprocess
import sys
from pathlib import Path

REPO: Path = Path(__file__).resolve().parents[1]
SCRIPT: Path = REPO / "scripts" / "cache_cleanup.py"


def classify(tmp_path: Path) -> dict[str, list[str]]:
    """Run the script against a scratch directory and read its verdict."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    keep: list[str] = []
    prune: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("保留："):
            keep.append(line[len("保留："):])
        elif line.startswith("可删除："):
            prune.append(line[len("可删除："):])
    return {"keep": keep, "prune": prune}


def test_runtime_config_survives(tmp_path: Path) -> None:
    """The startup files must not be offered for deletion."""
    for name in ["vt_setting.json", "wechat_setting.json", "autonomous.json"]:
        (tmp_path / name).write_text("{}", encoding="utf-8")

    verdict = classify(tmp_path)

    assert set(verdict["keep"]) == {
        "vt_setting.json",
        "wechat_setting.json",
        "autonomous.json",
    }
    assert verdict["prune"] == []


def test_consumed_artifacts_are_prunable(tmp_path: Path) -> None:
    """Sweep output goes, the runtime files stay."""
    (tmp_path / "sw_rb5_mt1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "fetch_stdout.log").write_text("", encoding="utf-8")
    (tmp_path / "vt_setting.json").write_text("{}", encoding="utf-8")

    verdict = classify(tmp_path)

    # Reported in sorted order, so the log precedes the sweep output.
    assert verdict["prune"] == ["fetch_stdout.log", "sw_rb5_mt1.json"]
    assert verdict["keep"] == ["vt_setting.json"]


def test_apply_deletes_and_reports_the_size(tmp_path: Path) -> None:
    """--apply removes the prunable files and leaves the kept ones."""
    (tmp_path / "sw_rb5_mt1.json").write_text("x" * 512, encoding="utf-8")
    (tmp_path / "vt_setting.json").write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--apply"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    assert not (tmp_path / "sw_rb5_mt1.json").exists()
    assert (tmp_path / "vt_setting.json").exists()
    assert "已删除 1 个文件" in result.stdout


def test_missing_root_is_reported_not_raised(tmp_path: Path) -> None:
    """A wiped research directory is not a crash."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path / "absent")],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 2
    assert "没有找到目录" in result.stdout


def test_unknown_files_are_kept(tmp_path: Path) -> None:
    """Anything with a non-artifact suffix is treated as live data."""
    (tmp_path / "notes.yaml").write_text("a: 1", encoding="utf-8")

    verdict = classify(tmp_path)

    assert verdict["keep"] == ["notes.yaml"]
    assert verdict["prune"] == []
