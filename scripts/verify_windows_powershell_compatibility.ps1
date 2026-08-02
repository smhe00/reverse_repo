param(
    [Parameter(Mandatory = $true)][string]$Root
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "Windows PowerShell 5.1 or later is required."
}

$rootPath = [System.IO.Path]::GetFullPath($Root)
$files = @(
    Get-ChildItem -LiteralPath $rootPath -Filter "*.ps1"
    Get-ChildItem `
        -LiteralPath (Join-Path $rootPath "scripts") `
        -Filter "*.ps1"
)
$failures = @()
$strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        $file.FullName,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count) {
        $failures += "$($file.Name): $($parseErrors.Message -join '; ')"
        continue
    }
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    try {
        $text = $strictUtf8.GetString($bytes)
    }
    catch {
        $failures += "$($file.Name): file is not valid UTF-8"
        continue
    }
    $containsNonAscii = $text -match "[^\x00-\x7F]"
    $hasUtf8Bom = (
        $bytes.Length -ge 3 `
        -and $bytes[0] -eq 0xEF `
        -and $bytes[1] -eq 0xBB `
        -and $bytes[2] -eq 0xBF
    )
    if ($containsNonAscii -and -not $hasUtf8Bom) {
        $failures += (
            "$($file.Name): non-ASCII Windows PowerShell script " +
            "must use a UTF-8 BOM"
        )
    }
}
if ($failures.Count) {
    throw "Windows PowerShell compatibility failed:`n$($failures -join "`n")"
}
Write-Output "Windows PowerShell 5.1 compatibility passed."
