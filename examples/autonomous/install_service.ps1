<#
.SYNOPSIS
    Register the VeighNa autonomous runner as a Windows scheduled task.

.DESCRIPTION
    Wraps Register-ScheduledTask so the supervisor keeps running without a
    human at the keyboard: the task starts at logon and at boot, runs hidden,
    and restarts automatically when the process exits. The task action runs
    `python -m vnpy.autonomous run --config <path>`, which is the same entry
    point the interactive launcher uses.

.PARAMETER Action
    install   register or refresh the scheduled task (default)
    uninstall remove the scheduled task
    status    show the current task definition and last run result

.PARAMETER TaskName
    Scheduled task name. Defaults to VeighNaAutonomousTrading.

.PARAMETER ConfigPath
    Runner configuration file. Defaults to .vntrader/autonomous.json inside
    this repository.

.PARAMETER PythonPath
    Python interpreter for the task action. Defaults to the repository virtual
    environment when one exists, otherwise the first `python` on PATH.

.PARAMETER SkipPreflight
    Skip the import smoke test before registering the task.

.EXAMPLE
    .\install_service.ps1 install
    .\install_service.ps1 uninstall
    .\install_service.ps1 status
#>

[CmdletBinding()]
param(
    [ValidateSet("install", "uninstall", "status")]
    [string]$Action = "install",
    [string]$TaskName = "VeighNaAutonomousTrading",
    [string]$ConfigPath = "",
    [string]$PythonPath = "",
    [switch]$SkipPreflight
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# This script lives in <repo>/examples/autonomous, so the repo root is two levels up.
$repoRoot = (Get-Item -LiteralPath $PSScriptRoot).Parent.Parent.FullName

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $repoRoot ".vntrader\autonomous.json"
}

function Resolve-DefaultPython {
    <#
        The scheduled task must not depend on the shell that installs it, so
        never fall back to the current process. Prefer a repository virtual
        environment, then the first python.exe on PATH.
    #>
    $candidates = @(
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
        (Join-Path $repoRoot "venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    throw "No Python interpreter found. Pass -PythonPath explicitly."
}

if (-not $PythonPath) {
    $PythonPath = Resolve-DefaultPython
}

$taskArgs = "-m vnpy.autonomous run --config `"$ConfigPath`""

function Test-Preflight {
    param(
        [string]$Interpreter,
        [string]$Args
    )

    Write-Output "Preflight: $Interpreter $Args --help"
    $output = & $Interpreter -m vnpy.autonomous run --help 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Preflight failed: the interpreter cannot run 'python -m vnpy.autonomous'."
    }
    $output | Select-Object -First 3 | ForEach-Object { Write-Output $_ }
}

function Install-AutonomousTask {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "Config file not found: $ConfigPath. Copy examples/autonomous/autonomous.json and fill it in first."
    }

    if (-not $SkipPreflight) {
        Test-Preflight -Interpreter $PythonPath -Args $taskArgs
    }

    $action = New-ScheduledTaskAction -Execute $PythonPath -Argument $taskArgs

    # Start both at logon and at boot: the logon trigger covers interactive
    # desktops, the boot trigger keeps the task armed on headless machines.
    $triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $triggerBoot = New-ScheduledTaskTrigger -AtStartup

    # Restart on any exit code after a short delay so a crash in the night
    # session does not stop the system until the next logon. Zero execution
    # time limit removes the default three day cutoff.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -Hidden

    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Highest

    # Register-ScheduledTask writes errors to the error stream and still
    # exits 0, so Out-Null alone would report a false success on a machine
    # where the current user cannot register highest-privilege tasks.
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description "VeighNa autonomous trading supervisor (config: $ConfigPath)" `
        -Action $action `
        -Trigger @($triggerLogon, $triggerBoot) `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null

    $registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $registered) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal0 = New-Object Security.Principal.WindowsPrincipal($identity)
        $isAdmin = $principal0.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            throw "Failed to register '$TaskName': RunLevel Highest requires an elevated PowerShell. Re-run this script as Administrator."
        }
        throw "Failed to register '$TaskName'."
    }

    Write-Output "Installed task '$TaskName'."
    Write-Output "Action   : $PythonPath $taskArgs"
    Write-Output "Triggers : at logon and at startup"
    Write-Output "Behaviour: hidden window, restart on exit, no execution time limit"
    Write-Output "Inspect  : .\install_service.ps1 status"
}

function Uninstall-AutonomousTask {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Output "Task '$TaskName' is not registered."
        return
    }

    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed task '$TaskName'."
}

function Show-AutonomousTaskStatus {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Output "Task '$TaskName' is not registered."
        return
    }

    $info = $task | Get-ScheduledTaskInfo
    Write-Output "TaskName   : $TaskName"
    Write-Output "State      : $($task.State)"
    Write-Output "LastRun    : $($info.LastRunTime)"
    Write-Output "NextRun    : $($info.NextRunTime)"
    Write-Output "LastResult : $($info.LastTaskResult)"
    foreach ($action in $task.Actions) {
        Write-Output "Execute    : $($action.Execute)"
        Write-Output "Arguments  : $($action.Arguments)"
    }
    foreach ($trigger in $task.Triggers) {
        $type = $trigger.CimClass.CimClassName -replace "^MSFT_Task", ""
        Write-Output "Trigger    : $type"
    }
}

switch ($Action) {
    "install" { Install-AutonomousTask }
    "uninstall" { Uninstall-AutonomousTask }
    "status" { Show-AutonomousTaskStatus }
}
