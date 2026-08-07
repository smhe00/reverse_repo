[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [datetime]$SignalDate = [datetime]::MinValue,
    [int]$Amount = 100000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$taskName = "miniQMT GC001 Signal Simulation"
$wrapper = Join-Path `
    $PSScriptRoot `
    "run_gc001_signal_simulation.ps1"
$now = Get-Date

if ($SignalDate -eq [datetime]::MinValue) {
    $candidate = $now.Date
    while (
        $candidate.DayOfWeek -in @(
            [DayOfWeek]::Saturday,
            [DayOfWeek]::Sunday
        ) `
        -or $candidate.AddHours(9).AddMinutes(27).AddSeconds(30) -le $now
    ) {
        $candidate = $candidate.AddDays(1)
    }
    $SignalDate = $candidate
}
$SignalDate = $SignalDate.Date
if ($SignalDate.DayOfWeek -in @(
    [DayOfWeek]::Saturday,
    [DayOfWeek]::Sunday
)) {
    throw "Signal simulation date must be a weekday: $($SignalDate.ToString('yyyy-MM-dd'))"
}
$startAt = $SignalDate.AddHours(9).AddMinutes(27).AddSeconds(30)
if ($startAt -le $now) {
    throw "Signal simulation trigger is not in the future: $startAt"
}
if ($Amount -lt 10000 -or $Amount -gt 1000000 -or ($Amount % 100) -ne 0) {
    throw "Signal simulation amount must be 10,000-1,000,000 yuan in 100-yuan steps."
}
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
    throw "Signal simulation wrapper is missing: $wrapper"
}
$simulationQmtPath = Get-ReverseRepoSimulationQmtPath
if ([string]$simulationQmtPath -notlike "*模拟*") {
    throw (
        "Signal simulation deployment rejected a non-simulation QMT path: " +
        $simulationQmtPath
    )
}
$simulationBindingPath = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
if (-not (Test-Path -LiteralPath $simulationBindingPath -PathType Leaf)) {
    throw "Simulation account binding is missing: $simulationBindingPath"
}
$binding = Read-ReverseRepoJson -Path $simulationBindingPath
$simulationEntries = @(
    $binding.accounts |
        Where-Object {
            $_.environment -eq "simulation" `
            -and $_.account_type -eq "SECURITY_ACCOUNT"
        }
)
if ($simulationEntries.Count -ne 1) {
    throw "Exactly one simulation security-account binding is required."
}

if (-not $PSCmdlet.ShouldProcess(
    $taskName,
    "Register one-time GC001 signal simulation task"
)) {
    return
}

$powerShellPath = Get-ReverseRepoPowerShell
$userId = (
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$action = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument (
        '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Amount {1} -ExecModel all' -f `
            $wrapper,
            $Amount
    ) `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At $startAt
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description (
        "One-time simulation-only GC001 morning book-signal validation: " +
        "09:27:30-09:35:30, small limit sell in the simulation account " +
        "when the eat/wallgone signal fires, cancel by 09:31:30 if unfilled."
    ) `
    -Force |
    Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $taskName
    State = [string]$task.State
    Trigger = $task.Triggers[0].StartBoundary
    AmountYuan = $Amount
    NextRunTime = $info.NextRunTime
    Action = $task.Actions[0].Arguments
}
