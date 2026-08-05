Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoSimulationQmtPath
$validationFirstExecutionTime = if ($args.Count -ge 1) { $args[0] } else { "" }
$validationSecondExecutionTime = if ($args.Count -ge 2) { $args[1] } else { "" }
$afternoonExecution = if ([string]::IsNullOrWhiteSpace(
    $validationSecondExecutionTime
)) {
    Get-ReverseRepoAfternoonExecutionTime
}
else {
    [TimeSpan]::Parse($validationSecondExecutionTime)
}
$firstExecution = if ([string]::IsNullOrWhiteSpace(
    $validationFirstExecutionTime
)) {
    Get-ReverseRepoFirstExecutionTime
}
else {
    [TimeSpan]::Parse($validationFirstExecutionTime)
}
$afternoonCashUsageRatio = Get-ReverseRepoAfternoonCashUsageRatio
$validationCashUsageRatio = if ($afternoonCashUsageRatio -eq 0) {
    1.0
}
else {
    $afternoonCashUsageRatio
}
$validationCashUsageRatioText = [string]::Format(
    [Globalization.CultureInfo]::InvariantCulture,
    "{0:R}",
    $validationCashUsageRatio
)
$afternoonExecutionTime = Format-ReverseRepoClockTime $afternoonExecution
$firstExecutionTime = Format-ReverseRepoClockTime $firstExecution
$afternoonConnectTime = Format-ReverseRepoClockTime `
    (Get-ReverseRepoTaskStartTime `
        -ExecutionTime $afternoonExecution `
        -LeadSeconds 60)
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
    "afternoon_$dateStamp.journal.json"
$mutexPath = Join-Path `
    $validationDirectory `
    "simulation_normal_execution.lock"
$logPath = Join-Path `
    $validationDirectory `
    "afternoon_$dateStamp.log"

New-Item -ItemType Directory -Force -Path $validationDirectory |
    Out-Null
foreach ($path in @($pythonPath, $bindingPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Simulation validation dependency is missing: $path"
    }
}

$alertArguments = @()
$alertEnabled = Enable-ReverseRepoOptionalFailureEmail `
    -ConfigPath $alertConfigPath `
    -SecretPath $alertSecretPath
if ($alertEnabled) {
    $alertArguments = @("--alert-config", $alertConfigPath)
}
try {
    & $pythonPath `
    (Join-Path $PSScriptRoot "gc001_r001_live_afternoon_sweep.py") `
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
    $afternoonExecutionTime `
    "--first-execution-time" `
    $firstExecutionTime `
    "--cash-usage-ratio" `
    $validationCashUsageRatioText `
        "--connect-time" `
        $afternoonConnectTime `
        @alertArguments *>> $logPath
    $result = $LASTEXITCODE
}
finally {
    Disable-ReverseRepoOptionalFailureEmail
}
if ($null -eq $result) {
    throw "Simulation afternoon executor returned no exit code."
}
exit ([int]$result)
