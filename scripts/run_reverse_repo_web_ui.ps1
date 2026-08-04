[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$pythonPath = Get-ReverseRepoPython
$serverPath = Join-Path $PSScriptRoot "reverse_repo_web_ui.py"
if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "Local UI server is missing: $serverPath"
}

$arguments = @(
    $serverPath,
    "--repo-root", $repoRoot,
    "--port", ([string]$Port)
)
if ($NoBrowser) {
    $arguments += "--no-browser"
}
& $pythonPath @arguments
exit $LASTEXITCODE
