Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoQmtPath -Environment "simulation"
$morningExecutionTime = Format-ReverseRepoClockTime `
    (Get-ReverseRepoMorningExecutionTime)
$null = Get-ReverseRepoSecondExecutionTime
$morningCashUsageRatio = Get-ReverseRepoMorningCashUsageRatio
$validationCashUsageRatio = if ($morningCashUsageRatio -eq 0) {
    1.0
}
else {
    $morningCashUsageRatio
}
$morningCashUsageRatioText = [string]::Format(
    [Globalization.CultureInfo]::InvariantCulture,
    "{0:R}",
    $validationCashUsageRatio
)
$bindingPath = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
$tradeDate = Get-Date -Format "yyyy-MM-dd"
$dateStamp = Get-Date -Format "yyyyMMdd"
$validationDirectory = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\simulation_validation"
$journalPath = Join-Path `
    $validationDirectory `
    "morning_recovery_$dateStamp.journal.json"
$mutexPath = Join-Path `
    $validationDirectory `
    "simulation_execution.lock"
$logPath = Join-Path `
    $validationDirectory `
    "morning_recovery_$dateStamp.log"

New-Item -ItemType Directory -Force -Path $validationDirectory |
    Out-Null
foreach ($path in @($pythonPath, $bindingPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Simulation validation dependency is missing: $path"
    }
}

& $pythonPath `
    (Join-Path $PSScriptRoot "prepare_repo_simulation_morning_recovery.py") `
    "--qmt-path" `
    $qmtPath `
    "--trade-date" `
    $tradeDate `
    "--journal" `
    $journalPath `
    "--account-binding" `
    $bindingPath `
    "--mutex" `
    $mutexPath `
    "--execution-time" `
    $morningExecutionTime `
    "--cash-usage-ratio" `
    $morningCashUsageRatioText *>> $logPath
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Simulation crash-boundary preparation failed."
}

& $pythonPath `
    (Join-Path $PSScriptRoot "gc001_live_daily_90pct_093042.py") `
    "--qmt-path" `
    $qmtPath `
    "--trade-date" `
    $tradeDate `
    "--journal" `
    $journalPath `
    "--account-binding" `
    $bindingPath `
    "--environment" `
    "simulation" `
    "--mutex" `
    $mutexPath `
    "--maximum-principal-yuan" `
    "1000" `
    "--execution-time" `
    $morningExecutionTime `
    "--cash-usage-ratio" `
    $morningCashUsageRatioText *>> $logPath
$result = $LASTEXITCODE
if ($null -eq $result) {
    throw "Simulation morning executor returned no exit code."
}
exit ([int]$result)
