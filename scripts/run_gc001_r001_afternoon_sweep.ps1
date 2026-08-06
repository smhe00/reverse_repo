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

$afternoonExecution = Get-ReverseRepoAfternoonExecutionTime
$firstExecution = Get-ReverseRepoFirstExecutionTime
$afternoonCashUsageRatio = Get-ReverseRepoAfternoonCashUsageRatio
if ($afternoonCashUsageRatio -eq 0) {
    Write-Output (
        "Second reverse-repo execution is disabled by " +
        "second_cash_usage_ratio=0."
    )
    exit 0
}
# Fail closed before resolving a QMT path or opening a broker connection.
Assert-ReverseRepoLiveEnableManifest
if ([string]::IsNullOrWhiteSpace($QmtPath)) {
$QmtPath = Get-ReverseRepoLiveQmtPath
}
$afternoonExecutionTime = Format-ReverseRepoClockTime $afternoonExecution
$firstExecutionTime = Format-ReverseRepoClockTime $firstExecution
$afternoonConnectTime = Format-ReverseRepoClockTime `
    (Get-ReverseRepoTaskStartTime `
        -ExecutionTime $afternoonExecution `
        -LeadSeconds 60)

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
    "reports\gc001_intraday\afternoon_sweep_v2"
$logDirectory = Join-Path $repoRoot "logs"
$journalPath = Join-Path `
    $reportDirectory `
    "repo_afternoon_sweep_$dateStamp.journal.json"
$mutexPath = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\reverse_repo_execution.lock"
$stdoutPath = Join-Path `
    $logDirectory `
    "repo_afternoon_sweep_$dateStamp.stdout.log"
$stderrPath = Join-Path `
    $logDirectory `
    "repo_afternoon_sweep_$dateStamp.stderr.log"
$strategyPath = Join-Path `
    $PSScriptRoot `
    "gc001_r001_live_afternoon_sweep.py"
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
    $afternoonExecutionTime,
    "--first-execution-time",
    $firstExecutionTime,
    "--cash-usage-ratio",
    ([string]::Format(
        [Globalization.CultureInfo]::InvariantCulture,
        "{0:R}",
        $afternoonCashUsageRatio
    )),
    "--connect-time",
    $afternoonConnectTime
)

$alertEnabled = Enable-ReverseRepoOptionalFailureEmail `
    -ConfigPath $AlertConfig `
    -SecretPath $alertSecretPath
if ($alertEnabled) {
    $arguments += @("--alert-config", $AlertConfig)
}
$previousErrorActionPreference = $ErrorActionPreference
# Windows PowerShell 5.1 turns native stderr redirected with 2> into error
# records; under ErrorActionPreference=Stop the first stderr line becomes a
# terminating error and is never written to the log. Capture with Continue so
# the strategy's failure reason lands in stderr.log.
$ErrorActionPreference = "Continue"
try {
    & $pythonPath @arguments 1> $stdoutPath 2> $stderrPath
    if ($null -eq $LASTEXITCODE) {
        throw "Python process did not provide an exit code."
    }
    $processExitCode = [int]$LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Disable-ReverseRepoOptionalFailureEmail
}
exit $processExitCode
