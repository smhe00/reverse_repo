Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoSimulationQmtPath
$bindingPath = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
$alertConfigPath = Join-Path `
    $repoRoot `
    "config\repo_failure_email.local.json"
$alertSecretPath = Join-Path `
    $repoRoot `
    "config\repo_failure_email_secret.local.clixml"
$tradeDate = Get-Date -Format "yyyy-MM-dd"
$dateStamp = Get-Date -Format "yyyyMMdd"
$reportDirectory = Join-Path `
    $repoRoot `
    "reports\simulation_interface_stress"
$outputPath = Join-Path $reportDirectory "stress_5hz_$dateStamp.json"
$checkpointPath = Join-Path `
    $reportDirectory `
    "stress_5hz_$dateStamp.checkpoint.json"
$samplesPath = Join-Path `
    $reportDirectory `
    "stress_5hz_$dateStamp.samples.jsonl"
$logPath = Join-Path $reportDirectory "stress_5hz_$dateStamp.log"
$mutexPath = Join-Path $reportDirectory "simulation_stress.lock"

New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
foreach ($path in @(
    $pythonPath,
    $bindingPath
)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Simulation stress dependency is missing: $path"
    }
}

$arguments = @(
    (Join-Path $PSScriptRoot "repo_simulation_interface_stress.py"),
    "--qmt-path", $qmtPath,
    "--account-binding", $bindingPath,
    "--trade-date", $tradeDate,
    "--output", $outputPath,
    "--checkpoint", $checkpointPath,
    "--samples", $samplesPath,
    "--mutex", $mutexPath,
    "--frequency-hz", "5",
    "--morning-start", "09:42:00",
    "--morning-end", "11:30:00",
    "--afternoon-start", "13:00:00",
    "--afternoon-end", "15:05:00",
    "--stop-new-orders", "14:50:00",
    "--trade-interval-minutes", "20"
)
$alertEnabled = Enable-ReverseRepoOptionalNotifications
if ($alertEnabled) {
    $arguments += @("--alert-config", $alertConfigPath)
}
try {
    & $pythonPath @arguments *>> $logPath
    if ($null -eq $LASTEXITCODE) {
        throw "Simulation stress executor returned no exit code."
    }
    $processExitCode = [int]$LASTEXITCODE
}
finally {
    Disable-ReverseRepoOptionalNotifications
}
exit $processExitCode
