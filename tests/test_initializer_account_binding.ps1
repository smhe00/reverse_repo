Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$initializerPath = Join-Path `
    $repoRoot `
    "scripts\initialize_reverse_repo.ps1"
$source = Get-Content -LiteralPath $initializerPath -Raw
$start = $source.IndexOf("function Initialize-AccountBinding")
$end = $source.IndexOf("Assert-LiveTasksInactive", $start)
if ($start -lt 0 -or $end -le $start) {
    throw "Unable to isolate Initialize-AccountBinding for testing."
}

$definition = $source.Substring($start, $end - $start)
Invoke-Expression $definition

# Use a path with no binding files and answer N without touching QMT or tasks.
$repoRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_binding_test_" + [guid]::NewGuid().ToString("N"))
function Read-Host { return "n" }

$result = @(Initialize-AccountBinding -Environment "live")
if ($result.Count -ne 1 -or [bool]$result[0] -ne $false) {
    throw (
        "Skipping account binding must emit exactly one Boolean false; got: " +
        ($result -join ", ")
    )
}
Write-Output "Account-binding Boolean gate test passed."
