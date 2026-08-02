Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$bundleRoot = $PSScriptRoot
$scriptsRoot = Join-Path $bundleRoot "scripts"
. (Join-Path $scriptsRoot "reverse_repo_runtime.ps1")
$windowsPowerShell = Get-ReverseRepoPowerShell
& $windowsPowerShell `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File (Join-Path `
        $scriptsRoot `
        "verify_windows_powershell_compatibility.ps1") `
    -Root $bundleRoot
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Windows PowerShell 5.1 compatibility verification failed."
}
$pythonPath = Get-ReverseRepoPython

# Validate the complete local F/S relationship and both cash ratios without
# opening a QMT connection. This catches malformed, negative, non-finite and
# greater-than-one ratios immediately.
$validatedFirstTime = Format-ReverseRepoClockTime `
    (Get-ReverseRepoFirstExecutionTime)
$validatedSecondTime = Format-ReverseRepoClockTime `
    (Get-ReverseRepoSecondExecutionTime)
$validatedFirstRatio = Get-ReverseRepoFirstCashUsageRatio
$validatedSecondRatio = Get-ReverseRepoSecondCashUsageRatio
Write-Output (
    "Runtime parameters valid: first=$validatedFirstTime/" +
    "$validatedFirstRatio; second=$validatedSecondTime/" +
    "$validatedSecondRatio"
)

$parseFailures = @()
$powerShellFiles = @(
    Get-ChildItem -LiteralPath $bundleRoot -Filter "*.ps1"
    Get-ChildItem -LiteralPath $scriptsRoot -Filter "*.ps1"
)
$powerShellFiles |
    ForEach-Object {
        $tokens = $null
        $errors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile(
            $_.FullName,
            [ref]$tokens,
            [ref]$errors
        )
        if ($errors.Count) {
            $parseFailures += [pscustomobject]@{
                File = $_.Name
                Errors = ($errors.Message -join "; ")
            }
        }
    }
if ($parseFailures.Count) {
    $parseFailures | Format-Table -AutoSize
    throw "PowerShell syntax verification failed."
}

$readmePath = Join-Path $bundleRoot "README.md"
$readmePdfPath = Join-Path `
    $bundleRoot `
    "output\pdf\reverse_repo_README.pdf"
$readmePdfHashPath = Join-Path `
    $bundleRoot `
    "output\pdf\reverse_repo_README.sha256"
if (-not (Test-Path -LiteralPath $readmePdfPath -PathType Leaf)) {
    throw "README PDF is missing. Run .\build_readme_pdf.ps1."
}
if (-not (Test-Path -LiteralPath $readmePdfHashPath -PathType Leaf)) {
    throw "README PDF hash is missing. Run .\build_readme_pdf.ps1."
}
$expectedReadmeHash = (
    Get-FileHash -LiteralPath $readmePath -Algorithm SHA256
).Hash.ToLowerInvariant()
$actualReadmeHash = (
    Get-Content -LiteralPath $readmePdfHashPath -Raw
).Trim().ToLowerInvariant()
if ($expectedReadmeHash -ne $actualReadmeHash) {
    throw "README PDF is stale. Run .\build_readme_pdf.ps1."
}

& $pythonPath `
    -m unittest discover `
    -s (Join-Path $bundleRoot "tests") `
    -p "test_*.py"
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Reverse-repo unit tests failed."
}

& $pythonPath `
    (Join-Path $scriptsRoot "verify_repo_state_machines.py") `
    "--output" `
    (Join-Path `
        $bundleRoot `
        "docs\reverse_repo_state_machine_verification.json")
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Reverse-repo formal verification failed."
}

Write-Output "reverse_repo verification passed."
