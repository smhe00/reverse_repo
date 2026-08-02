[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [datetime]$ValidationDate = [datetime]::MinValue
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$morningExecution = Get-ReverseRepoMorningExecutionTime
$afternoonExecution = Get-ReverseRepoAfternoonExecutionTime
$now = Get-Date
if ($ValidationDate -eq [datetime]::MinValue) {
    $candidate = $now.Date
    while (
        $candidate.DayOfWeek -in @(
            [DayOfWeek]::Saturday,
            [DayOfWeek]::Sunday
        ) `
        -or $candidate.Add($morningExecution).AddSeconds(-7) -le $now
    ) {
        $candidate = $candidate.AddDays(1)
    }
    $ValidationDate = $candidate
}
$ValidationDate = $ValidationDate.Date
if ($ValidationDate.DayOfWeek -in @(
    [DayOfWeek]::Saturday,
    [DayOfWeek]::Sunday
)) {
    throw (
        "Simulation certification date must be a weekday: " +
        $ValidationDate.ToString("yyyy-MM-dd")
    )
}
$powerShellPath = Get-ReverseRepoPowerShell
$userId = (
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
)

$definitions = @(
    [pscustomobject]@{
        Name = "miniQMT SIM Repo V2 First Recovery"
        Wrapper = Join-Path `
            $PSScriptRoot `
            "run_repo_simulation_morning_recovery_validation.ps1"
        At = $ValidationDate.Date.Add(
            $morningExecution - [TimeSpan]::FromSeconds(7)
        )
        LimitMinutes = 10
    },
    [pscustomobject]@{
        Name = "miniQMT SIM Repo V2 Second"
        Wrapper = Join-Path `
            $PSScriptRoot `
            "run_repo_simulation_afternoon_validation.ps1"
        At = $ValidationDate.Date.Add(
            $afternoonExecution - [TimeSpan]::FromSeconds(120)
        )
        LimitMinutes = 390
    },
    [pscustomobject]@{
        Name = "miniQMT SIM Repo V2 Certificate"
        Wrapper = Join-Path `
            $PSScriptRoot `
            "run_repo_simulation_certificate.ps1"
        At = $ValidationDate.Date.AddHours(15).AddMinutes(31)
        LimitMinutes = 10
    }
)

foreach ($definition in $definitions) {
    if ($definition.At -le $now) {
        throw "Validation trigger is not in the future: $($definition.At)"
    }
    if (-not (Test-Path -LiteralPath $definition.Wrapper -PathType Leaf)) {
        throw "Validation wrapper is missing: $($definition.Wrapper)"
    }
    if (-not $PSCmdlet.ShouldProcess(
        $definition.Name,
        "Register one-time simulation validation task"
    )) {
        continue
    }
    $action = New-ScheduledTaskAction `
        -Execute $powerShellPath `
        -Argument (
            '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f `
                $definition.Wrapper
        ) `
        -WorkingDirectory $repoRoot
    $trigger = New-ScheduledTaskTrigger -Once -At $definition.At
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (
            New-TimeSpan -Minutes $definition.LimitMinutes
        ) `
        -MultipleInstances IgnoreNew `
        -WakeToRun `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal `
        -UserId $userId `
        -LogonType Interactive `
        -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $definition.Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description (
            "One-time CNY 1,000 simulation-only validation for repo v2."
        ) `
        -Force |
        Out-Null
}

foreach ($obsoleteName in @(
    "miniQMT SIM Repo V2 Morning Recovery",
    "miniQMT SIM Repo V2 Afternoon"
)) {
    $obsolete = Get-ScheduledTask `
        -TaskName $obsoleteName `
        -ErrorAction SilentlyContinue
    if ($null -ne $obsolete -and $PSCmdlet.ShouldProcess(
        $obsoleteName,
        "Remove obsolete simulation validation task"
    )) {
        Unregister-ScheduledTask `
            -TaskName $obsoleteName `
            -Confirm:$false
    }
}

Get-ScheduledTask |
    Where-Object { $_.TaskName -like "miniQMT SIM Repo V2*" } |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        [pscustomobject]@{
            TaskName = $_.TaskName
            State = $_.State
            NextRunTime = $info.NextRunTime
        }
    }
