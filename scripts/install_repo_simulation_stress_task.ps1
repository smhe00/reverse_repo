[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [datetime]$StressDate = [datetime]::MinValue
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$taskName = "miniQMT SIM Interface Stress 5Hz"
$wrapper = Join-Path `
    $PSScriptRoot `
    "run_repo_simulation_interface_stress.ps1"
$now = Get-Date
if ($StressDate -eq [datetime]::MinValue) {
    $candidate = $now.Date
    while (
        $candidate.DayOfWeek -in @(
            [DayOfWeek]::Saturday,
            [DayOfWeek]::Sunday
        ) `
        -or $candidate.AddHours(9).AddMinutes(41).AddSeconds(30) -le $now
    ) {
        $candidate = $candidate.AddDays(1)
    }
    $StressDate = $candidate
}
$StressDate = $StressDate.Date
if ($StressDate.DayOfWeek -in @(
    [DayOfWeek]::Saturday,
    [DayOfWeek]::Sunday
)) {
    throw "Stress-test date must be a weekday: $($StressDate.ToString('yyyy-MM-dd'))"
}
$startAt = $StressDate.AddHours(9).AddMinutes(41).AddSeconds(30)
$measurementStart = $StressDate.Date.AddHours(9).AddMinutes(42)
$measurementEnd = $StressDate.Date.AddHours(15).AddMinutes(5)
$reservedMorningEnd = $StressDate.Date.AddHours(9).AddMinutes(40).AddSeconds(35)
$reservedAfternoonStart = $StressDate.Date.AddHours(15).AddMinutes(8)

if ($startAt -le $now) {
    throw "Stress-test trigger is not in the future: $startAt"
}
if ($measurementStart -lt $reservedMorningEnd.AddSeconds(60)) {
    throw "Stress test does not leave a one-minute morning isolation gap."
}
if ($measurementEnd -gt $reservedAfternoonStart.AddMinutes(-3)) {
    throw "Stress test does not leave a three-minute afternoon isolation gap."
}
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
    throw "Simulation stress wrapper is missing: $wrapper"
}
$simulationQmtPath = Get-ReverseRepoQmtPath -Environment "simulation"
if ([string]$simulationQmtPath -notlike "*模拟*") {
    throw (
        "Simulation stress deployment rejected a non-simulation QMT path: " +
        $simulationQmtPath
    )
}
$simulationBindingPath = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
if (-not (Test-Path -LiteralPath $simulationBindingPath -PathType Leaf)) {
    throw "Simulation account binding is missing: $simulationBindingPath"
}
$binding = Get-Content -LiteralPath $simulationBindingPath -Raw |
    ConvertFrom-Json
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
if ($null -ne $simulationEntries[0].PSObject.Properties["account_id"]) {
    throw "Plaintext account IDs are forbidden in simulation bindings."
}
if (-not $PSCmdlet.ShouldProcess(
    $taskName,
    "Register one-time simulation interface stress task"
)) {
    return
}

$pwsh = (Get-Command "pwsh.exe" -ErrorAction Stop).Source
$userId = (
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$action = New-ScheduledTaskAction `
    -Execute $pwsh `
    -Argument ('-NoProfile -File "{0}"' -f $wrapper) `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At $startAt
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 5 -Minutes 24) `
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
        "One-time simulation-only 5Hz interface stress test; " +
        "09:42-11:30 and 13:00-15:05, with controlled T+0 round trips."
    ) `
    -Force |
    Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $taskName
    State = [string]$task.State
    Trigger = $task.Triggers[0].StartBoundary
    MeasurementWindows = "09:42-11:30; 13:00-15:05"
    Frequency = "5Hz"
    NextRunTime = $info.NextRunTime
    Action = $task.Actions[0].Arguments
}
