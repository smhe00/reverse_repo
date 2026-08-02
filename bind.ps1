param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("live", "simulation")]
    [string]$Environment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$bundleRoot = $PSScriptRoot
$scriptsRoot = Join-Path $bundleRoot "scripts"
. (Join-Path $scriptsRoot "reverse_repo_runtime.ps1")

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoQmtPath -Environment $Environment
$label = if ($Environment -eq "live") {
    "repo_live"
}
else {
    "repo_simulation"
}
$filename = if ($Environment -eq "live") {
    "repo_live_account_binding.local.json"
}
else {
    "repo_simulation_account_binding.local.json"
}
$outputPath = Join-Path (Join-Path $bundleRoot "config") $filename

& $pythonPath `
    (Join-Path $scriptsRoot "bootstrap_repo_account_binding.py") `
    "--qmt-path" `
    $qmtPath `
    "--environment" `
    $Environment `
    "--label" `
    $label `
    "--output" `
    $outputPath
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "$Environment account binding failed."
}

Write-Output "$Environment account binding created: $outputPath"
