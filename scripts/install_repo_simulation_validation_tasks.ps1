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
function Get-FirstExecutionDeadlineTime {
    param([Parameter(Mandatory = $true)][TimeSpan]$ExecutionTime)
    $sessionEnd = if ($ExecutionTime -lt [TimeSpan]::FromHours(12)) {
        [TimeSpan]::FromHours(11.5)
    }
    else {
        [TimeSpan]::FromHours(15.5)
    }
    $fiveMinutesLater = $ExecutionTime + [TimeSpan]::FromMinutes(5)
    if ($fiveMinutesLater -lt $sessionEnd) {
        return $fiveMinutesLater
    }
    return $sessionEnd
}

function Find-IsolatedRecoveryExecutionTime {
    param(
        [Parameter(Mandatory = $true)][TimeSpan]$FirstExecution,
        [Parameter(Mandatory = $true)][TimeSpan]$SecondExecution,
        [TimeSpan]$EarliestTaskStart = [TimeSpan]::Zero
    )
    $normalStart = Get-ReverseRepoTaskStartTime `
        -ExecutionTime $FirstExecution `
        -LeadSeconds 162
    $normalEnd = Get-FirstExecutionDeadlineTime $FirstExecution
    $secondStart = Get-ReverseRepoTaskStartTime `
        -ExecutionTime $SecondExecution `
        -LeadSeconds 120
    $buffer = [TimeSpan]::FromSeconds(30)
    $candidates = @()
    $ranges = @(
        [pscustomobject]@{
            Start = [TimeSpan]::FromHours(13)
            End = [TimeSpan]::FromHours(15) + [TimeSpan]::FromMinutes(28)
        },
        [pscustomobject]@{
            Start = [TimeSpan]::FromHours(9.5)
            End = [TimeSpan]::FromHours(11) + [TimeSpan]::FromMinutes(28)
        }
    )
    foreach ($range in $ranges) {
        $candidate = $range.Start
        while ($candidate -le $range.End) {
            $candidates += $candidate
            $candidate += [TimeSpan]::FromMinutes(1)
        }
    }
    foreach ($candidate in $candidates) {
        $candidateStart = $candidate - [TimeSpan]::FromSeconds(162)
        if ($candidateStart -le $EarliestTaskStart) {
            continue
        }
        $candidateEnd = Get-FirstExecutionDeadlineTime $candidate
        $separateFromNormal = (
            $candidateEnd + $buffer -le $normalStart `
            -or $candidateStart -ge $normalEnd + $buffer
        )
        $separateFromSecond = (
            $candidateEnd + $buffer -le $secondStart
        )
        if ($separateFromNormal -and $separateFromSecond) {
            return $candidate
        }
    }
    throw (
        "No same-day trading window can isolate normal morning, fault " +
        "recovery and second normal validation for the configured times."
    )
}
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
$validationFirstExecution = $morningExecution
$validationSecondExecution = $afternoonExecution
if (
    $ValidationDate -eq $now.Date -and
    $ValidationDate.Add(
        (Get-ReverseRepoTaskStartTime `
            -ExecutionTime $validationFirstExecution `
            -LeadSeconds 162)
    ) -le $now
) {
    if ($now.TimeOfDay -lt [TimeSpan]::FromHours(13)) {
        $validationFirstExecution = [TimeSpan]::FromHours(13)
    }
    else {
        $candidate = $now.AddMinutes(4)
        $validationFirstExecution = New-TimeSpan `
            -Hours $candidate.Hour `
            -Minutes ($candidate.Minute + 1)
    }
}
if (
    $ValidationDate -eq $now.Date -and
    $validationFirstExecution -ge [TimeSpan]::FromHours(14.5) -and
    $validationSecondExecution -lt (
        [TimeSpan]::FromHours(15) + [TimeSpan]::FromMinutes(20)
    )
) {
    # Preserve enough isolated space late in the day for the normal first
    # path, crash recovery path and a useful second-path run before 15:30.
    $validationSecondExecution = (
        [TimeSpan]::FromHours(15) + [TimeSpan]::FromMinutes(20)
    )
}
if ($validationFirstExecution + [TimeSpan]::FromMinutes(5) -gt `
    $validationSecondExecution) {
    throw (
        "Today's remaining window cannot keep the second validation at " +
        (Format-ReverseRepoClockTime $validationSecondExecution) + "."
    )
}
$powerShellPath = Get-ReverseRepoPowerShell
$userId = (
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$morningNormalStart = Get-ReverseRepoTaskStartTime `
    -ExecutionTime $validationFirstExecution `
    -LeadSeconds 162
$recoveryExecution = Find-IsolatedRecoveryExecutionTime `
    -FirstExecution $validationFirstExecution `
    -SecondExecution $validationSecondExecution `
    -EarliestTaskStart $(
        if ($ValidationDate -eq $now.Date) {
            $now.TimeOfDay + [TimeSpan]::FromSeconds(30)
        }
        else {
            [TimeSpan]::Zero
        }
    )
$recoveryStart = Get-ReverseRepoTaskStartTime `
    -ExecutionTime $recoveryExecution `
    -LeadSeconds 162
$recoveryExecutionText = Format-ReverseRepoClockTime $recoveryExecution
$runDigit = (Get-Date -Format "s").Substring(18, 1)
$normalRemarkRoot = "repo_morn_no$runDigit"
$recoveryRemarkRoot = "repo_morn_re$runDigit"
$validationFirstExecutionText = Format-ReverseRepoClockTime `
    $validationFirstExecution
$validationSecondExecutionText = Format-ReverseRepoClockTime `
    $validationSecondExecution

$definitions = @(
    [pscustomobject]@{
        Name = "miniQMT SIM Repo V3 Morning Normal"
        Wrapper = Join-Path `
            $PSScriptRoot `
            "run_repo_simulation_morning_normal_validation.ps1"
        At = $ValidationDate.Date.Add($morningNormalStart)
        LimitMinutes = 10
        ExtraArguments = (
            ('-ValidationExecutionTime "{0}" ' +
             '-ValidationRemarkRoot "{1}"') -f `
                $validationFirstExecutionText, $normalRemarkRoot
        )
    },
    [pscustomobject]@{
        Name = "miniQMT SIM Repo V3 Afternoon Normal"
        Wrapper = Join-Path `
            $PSScriptRoot `
            "run_repo_simulation_afternoon_validation.ps1"
        At = $ValidationDate.Date.Add(
            $validationSecondExecution - [TimeSpan]::FromSeconds(120)
        )
        LimitMinutes = 390
        ExtraArguments = (
            '"{0}" "{1}"' -f `
                $validationFirstExecutionText, `
                $validationSecondExecutionText
        )
    },
    [pscustomobject]@{
        Name = "miniQMT SIM Repo V3 Morning Recovery"
        Wrapper = Join-Path `
            $PSScriptRoot `
            "run_repo_simulation_morning_recovery_validation.ps1"
        At = $ValidationDate.Date.Add($recoveryStart)
        LimitMinutes = 10
        ExtraArguments = (
            ('-RecoveryExecutionTime "{0}" ' +
             '-ValidationRemarkRoot "{1}"') -f `
                $recoveryExecutionText, $recoveryRemarkRoot
        )
    },
    [pscustomobject]@{
        Name = "miniQMT SIM Repo V3 Certificate"
        Wrapper = Join-Path `
            $PSScriptRoot `
            "run_repo_simulation_certificate.ps1"
        At = $ValidationDate.Date.AddHours(15).AddMinutes(31)
        LimitMinutes = 10
        ExtraArguments = (
            ('-ValidationDate "{0}" ' +
             '-ValidationFirstExecutionTime "{1}" ' +
             '-ValidationSecondExecutionTime "{2}"') -f `
                $ValidationDate.ToString("yyyy-MM-dd"), `
                $validationFirstExecutionText, `
                $validationSecondExecutionText
        )
    }
)

foreach ($definition in $definitions) {
    if ($definition.At -le $now) {
        throw "Validation trigger is not in the future: $($definition.At)"
    }
    if (-not (Test-Path -LiteralPath $definition.Wrapper -PathType Leaf)) {
        throw "Validation wrapper is missing: $($definition.Wrapper)"
    }
}

foreach ($definition in $definitions) {
    if (-not $PSCmdlet.ShouldProcess(
        $definition.Name,
        "Register one-time simulation validation task"
    )) {
        continue
    }
    $actionArguments = (
        '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f `
            $definition.Wrapper
    )
    if (-not [string]::IsNullOrWhiteSpace($definition.ExtraArguments)) {
        $actionArguments += " " + $definition.ExtraArguments
    }
    $action = New-ScheduledTaskAction `
        -Execute $powerShellPath `
        -Argument $actionArguments `
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
            "One-day isolated CNY 1,000 simulation validation for repo v3."
        ) `
        -Force |
        Out-Null
}

foreach ($obsoleteName in @(
    "miniQMT SIM Repo V2 First Recovery",
    "miniQMT SIM Repo V2 Second",
    "miniQMT SIM Repo V2 Certificate",
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
    Where-Object { $_.TaskName -like "miniQMT SIM Repo V3*" } |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        [pscustomobject]@{
            TaskName = $_.TaskName
            State = $_.State
            NextRunTime = $info.NextRunTime
        }
    }
