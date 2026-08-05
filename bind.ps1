param(
    [switch]$SkipIfExists
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$bundleRoot = $PSScriptRoot
$scriptsRoot = Join-Path $bundleRoot "scripts"
. (Join-Path $scriptsRoot "reverse_repo_runtime.ps1")

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoLiveQmtPath
$label = "repo_live"
$filename = "repo_live_account_binding.local.json"
$outputPath = Join-Path (Join-Path $bundleRoot "config") $filename

if (
    $SkipIfExists `
    -and (Test-Path -LiteralPath $outputPath -PathType Leaf)
) {
    Write-Output "Live account binding already exists: $outputPath"
    exit 0
}

& $pythonPath `
    (Join-Path $scriptsRoot "bootstrap_repo_account_binding.py") `
    "--qmt-path" `
    $qmtPath `
    "--environment" `
    "live" `
    "--label" `
    $label `
    "--output" `
    $outputPath
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Live account binding failed."
}

Write-Output "Live account binding created: $outputPath"
