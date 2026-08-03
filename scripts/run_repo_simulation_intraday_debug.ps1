[CmdletBinding()]
param(
    [datetime]$TradeDate = (Get-Date).Date,
    [string]$RecoveryTime = "13:00:10",
    [string]$StressStart = "13:05:00"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")

$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot
$pythonPath = Get-ReverseRepoPython
$powerShellPath = Get-ReverseRepoPowerShell
$qmtPath = Get-ReverseRepoQmtPath -Environment "simulation"
$liveQmtPath = Get-ReverseRepoQmtPath -Environment "live"
$bindingPath = Join-Path $repoRoot "config\repo_simulation_account_binding.local.json"
if ($TradeDate.Date -ne (Get-Date).Date) {
    throw "Intraday debug trade date must be today."
}
if ($qmtPath -ieq $liveQmtPath) {
    throw "Simulation and live QMT paths must be different."
}
foreach ($path in @($pythonPath, $bindingPath, $qmtPath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Intraday debug dependency is missing: $path"
    }
}

$dateStamp = $TradeDate.ToString("yyyyMMdd")
$tradeDateText = $TradeDate.ToString("yyyy-MM-dd")
$reportRoot = Join-Path $repoRoot "reports\gc001_intraday\intraday_debug_$dateStamp"
New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null
$recoveryJournal = Join-Path $reportRoot "recovery.journal.json"
$recoveryLog = Join-Path $reportRoot "recovery.log"
$recoveryMutex = Join-Path $reportRoot "recovery.lock"
$stressReport = Join-Path $reportRoot "stress.json"
$stressCheckpoint = Join-Path $reportRoot "stress.checkpoint.json"
$stressSamples = Join-Path $reportRoot "stress.samples.jsonl"
$stressLog = Join-Path $reportRoot "stress.log"
$stressMutex = Join-Path $reportRoot "stress.lock"
$afternoonJournal = Join-Path $repoRoot (
    "reports\gc001_intraday\simulation_validation\afternoon_$dateStamp.journal.json"
)
$afternoonLog = Join-Path $reportRoot "afternoon.log"
$summaryPath = Join-Path $reportRoot "summary.json"

foreach ($path in @($recoveryJournal, $stressReport, $afternoonJournal, $summaryPath)) {
    if (Test-Path -LiteralPath $path) {
        throw "Intraday debug refuses to overwrite existing evidence: $path"
    }
}

& $pythonPath `
    (Join-Path $PSScriptRoot "prepare_repo_simulation_morning_recovery.py") `
    --qmt-path $qmtPath `
    --trade-date $tradeDateText `
    --journal $recoveryJournal `
    --account-binding $bindingPath `
    --mutex $recoveryMutex `
    --execution-time $RecoveryTime `
    --cash-usage-ratio 1 `
    --remark-root repo_debug_m1 *>> $recoveryLog
if ($LASTEXITCODE -ne 0) {
    throw "Simulation fault-injection preparation failed: $LASTEXITCODE"
}

& $pythonPath `
    (Join-Path $PSScriptRoot "gc001_live_daily_90pct_093042.py") `
    --qmt-path $qmtPath `
    --trade-date $tradeDateText `
    --journal $recoveryJournal `
    --account-binding $bindingPath `
    --environment simulation `
    --mutex $recoveryMutex `
    --maximum-principal-yuan 1000 `
    --execution-time $RecoveryTime `
    --cash-usage-ratio 1 `
    --remark-root repo_debug_m1 *>> $recoveryLog
if ($LASTEXITCODE -notin @(0, 2)) {
    throw "Simulation production recovery failed: $LASTEXITCODE"
}

& $pythonPath `
    (Join-Path $PSScriptRoot "repo_simulation_interface_stress.py") `
    --qmt-path $qmtPath `
    --account-binding $bindingPath `
    --trade-date $tradeDateText `
    --output $stressReport `
    --checkpoint $stressCheckpoint `
    --samples $stressSamples `
    --mutex $stressMutex `
    --frequency-hz 5 `
    --morning-start 09:42:00 `
    --morning-end 11:30:00 `
    --afternoon-start $StressStart `
    --afternoon-end 15:05:00 `
    --stop-new-orders 14:50:00 `
    --trade-interval-minutes 20 `
    --partial-session *>> $stressLog
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Partial-session stress reported a failure; continuing to afternoon probe."
}

& $powerShellPath `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File (Join-Path $PSScriptRoot "run_repo_simulation_afternoon_validation.ps1") `
    *>> $afternoonLog
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Afternoon reverse-repo probe returned $LASTEXITCODE."
}

& $pythonPath `
    (Join-Path $PSScriptRoot "repo_simulation_intraday_debug_summary.py") `
    --recovery-journal $recoveryJournal `
    --stress-report $stressReport `
    --stress-samples $stressSamples `
    --afternoon-journal $afternoonJournal `
    --output $summaryPath
exit $LASTEXITCODE
