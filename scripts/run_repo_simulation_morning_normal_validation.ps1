param(
    [string]$ValidationExecutionTime = "",
    [string]$ValidationRemarkRoot = "repo_morn_norm"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoSimulationQmtPath
$morningExecutionTime = if ([string]::IsNullOrWhiteSpace(
    $ValidationExecutionTime
)) {
    Format-ReverseRepoClockTime (Get-ReverseRepoMorningExecutionTime)
}
else {
    $ValidationExecutionTime
}
$null = Get-ReverseRepoSecondExecutionTime
$configuredRatio = Get-ReverseRepoMorningCashUsageRatio
$validationRatio = if ($configuredRatio -eq 0) { 1.0 } else { $configuredRatio }
$ratioText = [string]::Format(
    [Globalization.CultureInfo]::InvariantCulture,
    "{0:R}",
    $validationRatio
)
$bindingPath = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
$alertConfigPath = Join-Path $repoRoot "config\repo_failure_email.local.json"
$alertSecretPath = Join-Path `
    $repoRoot `
    "config\repo_failure_email_secret.local.clixml"
$tradeDate = Get-Date -Format "yyyy-MM-dd"
$dateStamp = Get-Date -Format "yyyyMMdd"
$validationDirectory = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\simulation_validation"
$journalPath = Join-Path `
    $validationDirectory `
    "morning_normal_$dateStamp.journal.json"
$mutexPath = Join-Path `
    $validationDirectory `
    "simulation_normal_execution.lock"
$logPath = Join-Path `
    $validationDirectory `
    "morning_normal_$dateStamp.log"

New-Item -ItemType Directory -Force -Path $validationDirectory | Out-Null
foreach ($path in @($pythonPath, $bindingPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Simulation normal-path dependency is missing: $path"
    }
}
if (Test-Path -LiteralPath $journalPath) {
    throw (
        "Simulation morning normal-path journal already exists; " +
        "refusing to reuse evidence: $journalPath"
    )
}

$alertArguments = @()
$alertEnabled = Enable-ReverseRepoOptionalFailureEmail `
    -ConfigPath $alertConfigPath `
    -SecretPath $alertSecretPath
if ($alertEnabled) {
    $alertArguments = @("--alert-config", $alertConfigPath)
}
try {
    # This is the production morning Python entry point. Simulation-only flags
    # cap principal at CNY 1,000 and isolate the broker remark namespace.
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
    $ratioText `
        "--remark-root" `
        $ValidationRemarkRoot `
        @alertArguments *>> $logPath
    $result = $LASTEXITCODE
}
finally {
    Disable-ReverseRepoOptionalFailureEmail
}
if ($null -eq $result) {
    throw "Simulation morning normal-path executor returned no exit code."
}
exit ([int]$result)
