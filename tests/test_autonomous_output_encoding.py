"""Regression test for the CI cp1252 stdout failure.

CI runners default to cp1252 stdout, and the core engine prints Chinese
diagnostics with print(). Without UTF-8 output those prints raise
UnicodeEncodeError and take the trading child down before it starts.
"""

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def test_ensure_utf8_output_survives_cp1252_stdout(tmp_path) -> None:
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        "from vnpy.autonomous.runner import ensure_utf8_output\n"
        "try:\n"
        "    print('\\u4e2d\\u6587\\u6d4b\\u8bd5')\n"
        "except UnicodeEncodeError:\n"
        "    print('BEFORE_FAIL')\n"
        "ensure_utf8_output()\n"
        "print('\\u4e2d\\u6587\\u6d4b\\u8bd5')\n"
        "print('AFTER_OK')\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        env=env,
        cwd=str(tmp_path),
        timeout=120,
    )

    stdout: str = result.stdout.decode("utf-8", errors="replace")
    assert "BEFORE_FAIL" in stdout
    assert "AFTER_OK" in stdout
    assert "\u4e2d\u6587\u6d4b\u8bd5" in stdout


def test_ensure_utf8_output_tolerates_capture_streams() -> None:
    from vnpy.autonomous.runner import ensure_utf8_output

    class FakeStream:
        encoding = "cp1252"

    # 不支持 reconfigure 的对象不得抛出异常
    ensure_utf8_output()

    original_stdout = sys.stdout
    try:
        sys.stdout = FakeStream()      # type: ignore[assignment]
        ensure_utf8_output()
    finally:
        sys.stdout = original_stdout
