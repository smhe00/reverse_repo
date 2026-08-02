param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("morning", "afternoon")]
    [string]$Window
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoQmtPath -Environment "live"
$firstCashUsageRatio = Get-ReverseRepoFirstCashUsageRatio
$secondCashUsageRatio = Get-ReverseRepoSecondCashUsageRatio
$bindingPath = Join-Path `
    $repoRoot `
    "config\repo_live_account_binding.local.json"
$reportDirectory = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\live_readonly_validation"
$dateStamp = Get-Date -Format "yyyyMMdd"
$outputPath = Join-Path `
    $reportDirectory `
    "${Window}_$dateStamp.json"
$logPath = Join-Path `
    $reportDirectory `
    "${Window}_$dateStamp.log"
$mutexPath = Join-Path `
    $reportDirectory `
    "live_readonly.lock"

New-Item -ItemType Directory -Force -Path $reportDirectory |
    Out-Null
foreach ($path in @($pythonPath, $bindingPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Live read-only dependency is missing: $path"
    }
}

& $pythonPath `
    (Join-Path $PSScriptRoot "repo_live_readonly_preflight.py") `
    "--qmt-path" `
    $qmtPath `
    "--account-binding" `
    $bindingPath `
    "--output" `
    $outputPath `
    "--mutex" `
    $mutexPath `
    "--maximum-quote-age-seconds" `
    "3" `
    "--first-cash-usage-ratio" `
    ([string]::Format(
        [Globalization.CultureInfo]::InvariantCulture,
        "{0:R}",
        $firstCashUsageRatio
    )) `
    "--second-cash-usage-ratio" `
    ([string]::Format(
        [Globalization.CultureInfo]::InvariantCulture,
        "{0:R}",
        $secondCashUsageRatio
    )) *>> $logPath
$result = $LASTEXITCODE
if ($null -eq $result) {
    throw "Live read-only rehearsal returned no exit code."
}
exit ([int]$result)
