param(
    [string]$TradeDate = "",
    [int]$Amount = 100000,
    [ValidateSet("static", "trail", "tranche", "all")]
    [string]$ExecModel = "all",
    [switch]$Smoke
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoSimulationQmtPath
if ([string]$qmtPath -notlike "*模拟*") {
    throw (
        "Simulation signal test requires a simulation QMT path; " +
        "got: $qmtPath"
    )
}
if ([string]::IsNullOrWhiteSpace($TradeDate)) {
    $TradeDate = Get-Date -Format "yyyy-MM-dd"
}
$dateStamp = Get-Date -Format "yyyyMMdd"
$scriptPath = Join-Path $PSScriptRoot "gc001_live_microprice_validation.py"
$bindingPath = Join-Path $repoRoot "config\repo_simulation_account_binding.local.json"
$logDirectory = Join-Path $repoRoot "logs"
$outputRoot = Join-Path $repoRoot "reports\gc001_signal_simulation"
$logPath = Join-Path $logDirectory "gc001_signal_simulation_$dateStamp.log"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
foreach ($path in @($pythonPath, $scriptPath, $bindingPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Simulation signal test dependency is missing: $path"
    }
}

$arguments = @(
    $scriptPath,
    "--trade-date", $TradeDate,
    "--mode", "sim",
    "--qmt-path", $qmtPath,
    "--account-binding", $bindingPath,
    "--amount", [string]$Amount,
    "--exec-model", $ExecModel,
    "--output-root", $outputRoot
)
if ($Smoke) {
    $arguments += "--smoke"
}

& $pythonPath @arguments *>> $logPath
if ($null -eq $LASTEXITCODE) {
    throw "Simulation signal test returned no exit code."
}
exit [int]$LASTEXITCODE
