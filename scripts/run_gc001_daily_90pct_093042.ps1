param(
    [string]$QmtPath = "",
    [string]$AccountBinding = "",
    [string]$AlertConfig = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$morningExecutionTime = Format-ReverseRepoClockTime `
    (Get-ReverseRepoMorningExecutionTime)
# Validate the complete F/S relationship before any live connection.
$null = Get-ReverseRepoSecondExecutionTime
$morningCashUsageRatio = Get-ReverseRepoMorningCashUsageRatio
if ($morningCashUsageRatio -eq 0) {
    Write-Output (
        "First reverse-repo execution is disabled by " +
        "first_cash_usage_ratio=0."
    )
    exit 0
}
# Fail closed before resolving a QMT path or opening a broker connection.
Assert-ReverseRepoLiveEnableManifest
if ([string]::IsNullOrWhiteSpace($QmtPath)) {
$QmtPath = Get-ReverseRepoLiveQmtPath
}

if ([string]::IsNullOrWhiteSpace($AccountBinding)) {
    $AccountBinding = Join-Path `
        $repoRoot `
        "config\repo_live_account_binding.local.json"
}
if ([string]::IsNullOrWhiteSpace($AlertConfig)) {
    $AlertConfig = Join-Path `
        $repoRoot `
        "config\repo_failure_email.local.json"
}
$alertSecretPath = Join-Path `
    $repoRoot `
    "config\repo_failure_email_secret.local.clixml"
$tradeDate = Get-Date -Format "yyyy-MM-dd"
$dateStamp = Get-Date -Format "yyyyMMdd"
$reportDirectory = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\daily_90pct_v2"
$logDirectory = Join-Path $repoRoot "logs"
$journalPath = Join-Path `
    $reportDirectory `
    "gc001_daily_90pct_093042_$dateStamp.journal.json"
$mutexPath = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\reverse_repo_execution.lock"
$stdoutPath = Join-Path `
    $logDirectory `
    "gc001_daily_90pct_093042_$dateStamp.stdout.log"
$stderrPath = Join-Path `
    $logDirectory `
    "gc001_daily_90pct_093042_$dateStamp.stderr.log"
$strategyPath = Join-Path `
    $PSScriptRoot `
    "gc001_live_daily_90pct_093042.py"
$pythonPath = Get-ReverseRepoPython

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python executable does not exist: $pythonPath"
}
New-Item `
    -ItemType Directory `
    -Force `
    -Path $reportDirectory, $logDirectory |
    Out-Null

$arguments = @(
    $strategyPath,
    "--qmt-path",
    $QmtPath,
    "--trade-date",
    $tradeDate,
    "--journal",
    $journalPath,
    "--account-binding",
    $AccountBinding,
    "--environment",
    "live",
    "--mutex",
    $mutexPath,
    "--execution-time",
    $morningExecutionTime,
    "--cash-usage-ratio",
    ([string]::Format(
        [Globalization.CultureInfo]::InvariantCulture,
        "{0:R}",
        $morningCashUsageRatio
    ))
)

$alertEnabled = Enable-ReverseRepoOptionalFailureEmail `
    -ConfigPath $AlertConfig `
    -SecretPath $alertSecretPath
if ($alertEnabled) {
    $arguments += @("--alert-config", $AlertConfig)
}
try {
    & $pythonPath @arguments 1> $stdoutPath 2> $stderrPath
    if ($null -eq $LASTEXITCODE) {
        throw "Python process did not provide an exit code."
    }
    $processExitCode = [int]$LASTEXITCODE
}
finally {
    Disable-ReverseRepoOptionalFailureEmail
}
exit $processExitCode
