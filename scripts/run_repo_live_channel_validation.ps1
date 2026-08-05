[CmdletBinding()]
param(
    [string]$Confirmation = "",
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")

$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot
$managedTaskNames = @(
    "miniQMT Reverse Repo First",
    "miniQMT Reverse Repo Second"
)
foreach ($name in $managedTaskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -ne $task -and [string]$task.State -ne "Disabled") {
        throw "Live task is not Disabled: $name. Run .\rr off first."
    }
}
$manifestPath = Get-ReverseRepoLiveEnableManifestPath
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    throw "Live-enable snapshot still exists. Run .\rr off first."
}

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoLiveQmtPath
$bindingPath = Join-Path $repoRoot "config\repo_live_account_binding.local.json"
$signingKeyPath = Join-Path $repoRoot "config\repo_release_gate_secret.local.json"
$validatorPath = Join-Path $PSScriptRoot "repo_live_channel_validation.py"
$executorPath = Join-Path $PSScriptRoot "gc001_live_daily_90pct_093042.py"
$verifyPath = Join-Path $repoRoot "verify.ps1"
$alertConfigPath = Join-Path $repoRoot "config\repo_failure_email.local.json"
$alertSecretPath = Join-Path $repoRoot "config\repo_failure_email_secret.local.clixml"
$reportDirectory = Join-Path $repoRoot "reports\gc001_intraday\live_channel_validation"
$dateStamp = Get-Date -Format "yyyyMMdd"
$tradeDate = Get-Date -Format "yyyy-MM-dd"
$attemptStamp = Get-Date -Format "HHmmss"
$journalPath = Join-Path `
    $reportDirectory `
    "live_channel_${dateStamp}_${attemptStamp}.journal.json"
if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
    throw (
        "Certification journal already exists for this second: " +
        "$journalPath. Wait a second and retry."
    )
}
$remarkPrefix = "repo_live_cert_${dateStamp}_${attemptStamp}_"
$preflightPath = Join-Path $reportDirectory "preflight_$dateStamp.json"
$certificatePath = Join-Path $reportDirectory "latest.json"
$mutexPath = Join-Path $repoRoot "reports\gc001_intraday\reverse_repo_execution.lock"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $mutexPath) |
    Out-Null
$resultReportPath = Join-Path `
    $reportDirectory `
    ("result_{0}.json" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$executorStarted = $false

trap {
    $reason = [string]$_.Exception.Message
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    $failureReport = [ordered]@{
        schema_version = 1
        certificate_type = "live_channel"
        passed = $false
        occurred_at = [datetimeoffset]::Now.ToString("o")
        reason = $reason
        journal = [System.IO.Path]::GetFileName($journalPath)
    } | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText(
        $resultReportPath,
        $failureReport,
        (New-Object System.Text.UTF8Encoding($false))
    )
    # Routine operational guards are not trading faults; do not raise a
    # failure alert for them (outside the certification window, a pre-existing
    # certificate, tasks not disabled, a cancelled confirmation, etc.).
    $guardReasons = @(
        "*A live-channel certificate already exists*",
        "*当前不在快速实盘认证窗口*",
        "*current date is not an exchange trading day*",
        "*Confirmation did not match*",
        "*Live task is not Disabled*",
        "*Live-enable snapshot still exists*",
        "*Global reverse-repo mutex is busy*"
    )
    $isGuardReason = $false
    foreach ($pattern in $guardReasons) {
        if ($reason -like $pattern) {
            $isGuardReason = $true
            break
        }
    }
    if (
        -not $executorStarted `
        -and -not $isGuardReason
    ) {
        try {
            $mailEnabled = Enable-ReverseRepoOptionalFailureEmail `
                -ConfigPath $alertConfigPath `
                -SecretPath $alertSecretPath
            if ($mailEnabled) {
                & $pythonPath $validatorPath notify-failure `
                    --alert-config $alertConfigPath `
                    --journal $journalPath `
                    --reason $reason
            }
        }
        catch {
            Write-Warning "Failure email could not be delivered: $($_.Exception.Message)"
        }
        finally {
            Disable-ReverseRepoOptionalFailureEmail
        }
    }
    [Console]::Error.WriteLine($reason)
    exit 1
}

try {
    $mutexProbe = [System.IO.File]::Open(
        $mutexPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    $mutexProbe.Dispose()
}
catch {
    throw "Global reverse-repo mutex is busy; certification is refused."
}

foreach ($requiredPath in @(
    $pythonPath,
    $bindingPath,
    $signingKeyPath,
    $validatorPath,
    $executorPath,
    $verifyPath
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Live-channel certification dependency is missing: $requiredPath"
    }
}
if (Test-Path -LiteralPath $certificatePath -PathType Leaf) {
    throw "A live-channel certificate already exists. Use .\rr cert stat or reset."
}
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null

# Fail fast outside the certification window: friendly message only, no full
# verification dump, no failure email, and no QMT connection.
& $pythonPath $validatorPath check-window
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    exit 1
}

Write-Output "Running full local verification; this step does not connect to QMT or submit orders."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $verifyPath
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Full local verification failed; live certification did not start."
}

& $pythonPath $validatorPath preflight `
    --qmt-path $qmtPath `
    --account-binding $bindingPath `
    --output $preflightPath
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Live read-only preflight failed; no order was submitted."
}
$preflight = Get-Content -LiteralPath $preflightPath -Raw | ConvertFrom-Json
Write-Output ""
Write-Output "Account label: $($preflight.account_label)"
Write-Output "Fixed cumulative fill limit: CNY 1,000"
Write-Output "Planned trigger: $($preflight.planned_trigger_at)"
Write-Output "Instrument: GC001 (204001.SH)"
Write-Output ""
if ($PreflightOnly) {
    Write-Output "Read-only preflight passed. No order was submitted."
    exit 0
}
if ([string]::IsNullOrWhiteSpace($Confirmation)) {
    $Confirmation = Read-Host "Type LIVE 1000 to authorize the real canary order; anything else cancels"
}
if ($Confirmation -cne "LIVE 1000") {
    throw "Confirmation did not match; no order was submitted."
}

$trigger = [datetimeoffset]::Parse([string]$preflight.planned_trigger_at)
$executionTime = $trigger.ToString("HH:mm:ss")
$arguments = @(
    $executorPath,
    "--qmt-path", $qmtPath,
    "--trade-date", $tradeDate,
    "--journal", $journalPath,
    "--account-binding", $bindingPath,
    "--environment", "live",
    "--mutex", $mutexPath,
    "--execution-time", $executionTime,
    "--cash-usage-ratio", "1",
    "--maximum-principal-yuan", "1000",
    "--remark-root", "repo_live_cert",
    "--remark-prefix", $remarkPrefix,
    "--live-channel-certification"
)
$alertEnabled = Enable-ReverseRepoOptionalFailureEmail `
    -ConfigPath $alertConfigPath `
    -SecretPath $alertSecretPath
try {
    $executorStarted = $true
    & $pythonPath @arguments
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        if ($alertEnabled) {
            & $pythonPath $validatorPath notify-failure `
                --alert-config $alertConfigPath `
                --journal $journalPath `
                --reason "Production state machine did not reach a certifiable state."
        }
        throw "Production state machine did not reach a certifiable state."
    }
    & $pythonPath $validatorPath certify `
        --qmt-path $qmtPath `
        --account-binding $bindingPath `
        --journal $journalPath `
        --preflight $preflightPath `
        --signing-key $signingKeyPath `
        --output $certificatePath
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        if ($alertEnabled) {
            & $pythonPath $validatorPath notify-failure `
                --alert-config $alertConfigPath `
                --journal $journalPath `
                --reason "Broker evidence and journal reconciliation failed; no certificate issued."
        }
        throw "Broker evidence and journal reconciliation failed; no certificate issued."
    }
    $certificate = Get-Content -LiteralPath $certificatePath -Raw |
        ConvertFrom-Json
    $successReport = [ordered]@{
        schema_version = 1
        certificate_type = "live_channel"
        passed = $true
        completed_at = [datetimeoffset]::Now.ToString("o")
        filled_principal_yuan = [int]$certificate.filled_principal_yuan
        certificate = [System.IO.Path]::GetFileName($certificatePath)
        journal = [System.IO.Path]::GetFileName($journalPath)
    } | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText(
        $resultReportPath,
        $successReport,
        (New-Object System.Text.UTF8Encoding($false))
    )

    # Archive stale certification evidence from earlier attempts on success,
    # keeping only this attempt's certificate, journal, preflight and report.
    $keepPaths = @(
        $journalPath,
        $preflightPath,
        $certificatePath,
        $resultReportPath
    ) | ForEach-Object {
        [System.IO.Path]::GetFullPath($_)
    }
    $staleItems = @(
        Get-ChildItem `
            -LiteralPath $reportDirectory `
            -File `
            -ErrorAction SilentlyContinue |
            Where-Object {
                [System.IO.Path]::GetFullPath($_.FullName) `
                    -notin $keepPaths
            }
    )
    if ($staleItems.Count -gt 0) {
        $archiveDirectory = Join-Path `
            $reportDirectory `
            ("revoked\" + (Get-Date -Format "yyyyMMdd_HHmmss"))
        New-Item `
            -ItemType Directory `
            -Force `
            -Path $archiveDirectory |
            Out-Null
        foreach ($item in $staleItems) {
            Move-Item `
                -LiteralPath $item.FullName `
                -Destination $archiveDirectory
        }
        Write-Output "Stale certification evidence archived: $archiveDirectory"
    }

    if ($alertEnabled) {
        & $pythonPath $validatorPath notify-success `
            --alert-config $alertConfigPath `
            --journal $journalPath
    }
    Write-Output "Live-channel certification passed. Live tasks remain Disabled."
    Write-Output "Review .\rr cert stat, then run .\rr on manually."
}
finally {
    Disable-ReverseRepoOptionalFailureEmail
}
