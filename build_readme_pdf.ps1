Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$bundleRoot = $PSScriptRoot
$builder = Join-Path $bundleRoot "scripts\build_readme_pdf.py"
$dependencyPython = Join-Path `
    $env:USERPROFILE `
    ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$candidates = @(
    $env:REVERSE_REPO_PDF_PYTHON,
    (Join-Path $bundleRoot ".venv\Scripts\python.exe"),
    $dependencyPython,
    (Join-Path $bundleRoot "..\.venv\Scripts\python.exe")
)
$pythonPath = $null
foreach ($candidate in $candidates) {
    if (
        -not [string]::IsNullOrWhiteSpace($candidate) `
        -and (Test-Path -LiteralPath $candidate -PathType Leaf)
    ) {
        & $candidate -c "import reportlab" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $pythonPath = $candidate
            break
        }
    }
}
if ($null -eq $pythonPath) {
    throw (
        "No Python runtime with reportlab was found. Set " +
        "REVERSE_REPO_PDF_PYTHON to a suitable python.exe."
    )
}
& $pythonPath $builder
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "README PDF generation failed."
}
