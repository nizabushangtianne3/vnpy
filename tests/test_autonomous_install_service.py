"""Read-only regression tests for the Windows scheduled task installer.

The installer is a PowerShell script, so these tests never register a real
task. They parse the script, assert the behaviours that keep unattended runs
honest, and run only the read-only subcommands (`status`, `uninstall` on a
name that cannot exist) against a throwaway task name.
"""

import shutil
import subprocess
import sys
from pathlib import Path


import pytest


REPO_ROOT: Path = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT: Path = REPO_ROOT / "examples" / "autonomous" / "install_service.ps1"

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not POWERSHELL,
    reason="Windows scheduled tasks only apply on Windows with PowerShell",
)


def _run_script(args: list[str]) -> subprocess.CompletedProcess:
    """Run the installer with the given arguments and capture output."""
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_SCRIPT),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def test_installer_script_exists() -> None:
    assert INSTALL_SCRIPT.is_file()


def test_installer_parses_without_syntax_errors() -> None:
    """A malformed script must fail loudly at install time, not silently."""
    code = (
        "$errors=$null; $tokens=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_SCRIPT}',[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Output $_.Message }; exit 1 } ; "
        "exit 0"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_installer_exposes_three_subcommands() -> None:
    source = INSTALL_SCRIPT.read_text(encoding="utf-8-sig")
    assert '"install"' in source
    assert '"uninstall"' in source
    assert '"status"' in source


def test_installer_does_not_reuse_the_calling_process_as_interpreter() -> None:
    """The scheduled task must outlive the shell that installed it."""
    source = INSTALL_SCRIPT.read_text(encoding="utf-8-sig")
    assert "Get-Process -Id $PID" not in source


def test_installer_defaults_to_repo_root_config() -> None:
    """examples/autonomous is two levels below the repo root."""
    source = INSTALL_SCRIPT.read_text(encoding="utf-8-sig")
    assert "Split-Path -Parent $PSScriptRoot" not in source
    assert ".Parent.Parent" in source


def test_installer_registers_logon_and_boot_triggers() -> None:
    source = INSTALL_SCRIPT.read_text(encoding="utf-8-sig")
    assert "New-ScheduledTaskTrigger -AtLogOn" in source
    assert "New-ScheduledTaskTrigger -AtStartup" in source


def test_installer_restarts_on_exit_and_runs_hidden() -> None:
    source = INSTALL_SCRIPT.read_text(encoding="utf-8-sig")
    assert "-RestartCount" in source
    assert "-Hidden" in source


def test_status_subcommand_reports_missing_task() -> None:
    result = _run_script(["-Action", "status", "-TaskName", "VnpyInstallerProbeNoTask"])
    assert result.returncode == 0
    assert "not registered" in result.stdout + result.stderr


def test_uninstall_subcommand_is_safe_on_missing_task() -> None:
    result = _run_script(
        ["-Action", "uninstall", "-TaskName", "VnpyInstallerProbeNoTask"]
    )
    assert result.returncode == 0
    assert "not registered" in result.stdout + result.stderr


def test_install_requires_config_file(tmp_path: Path) -> None:
    """Refuse to arm an unattended session with no config in place."""
    missing = tmp_path / "does_not_exist.json"
    result = _run_script(
        [
            "-Action",
            "install",
            "-TaskName",
            "VnpyInstallerProbeNoTask",
            "-ConfigPath",
            str(missing),
        ]
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Config file not found" in combined
