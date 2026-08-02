Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot "scripts\reverse_repo_runtime.ps1")

$testRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_utf8_json_" + [guid]::NewGuid().ToString("N"))
$validPath = Join-Path $testRoot "runtime.local.json"
$invalidPath = Join-Path $testRoot "invalid.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

try {
    $null = New-Item -ItemType Directory -Path $testRoot
    # Keep this test script itself ASCII-compatible for Windows PowerShell
    # 5.1, then construct the Chinese paths from Unicode code points.
    $liveName = -join @(
        [char]0x56FD, [char]0x91D1, [char]0x8BC1, [char]0x5238,
        [char]0x0051, [char]0x004D, [char]0x0054, [char]0x4EA4,
        [char]0x6613, [char]0x7AEF
    )
    $simulationName = -join @(
        [char]0x56FD, [char]0x91D1, [char]0x0051, [char]0x004D,
        [char]0x0054, [char]0x4EA4, [char]0x6613, [char]0x7AEF,
        [char]0x6A21, [char]0x62DF
    )
    $expectedLive = "D:\$liveName\userdata_mini"
    $expectedSimulation = "D:\$simulationName\userdata_mini"
    $validJson = [ordered]@{
        live_qmt_path = $expectedLive
        simulation_qmt_path = $expectedSimulation
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText($validPath, $validJson, $utf8NoBom)
    $parsed = Read-ReverseRepoJson -Path $validPath
    if ([string]$parsed.live_qmt_path -ne $expectedLive) {
        throw "BOM-less UTF-8 live QMT path was not preserved."
    }
    if ([string]$parsed.simulation_qmt_path -ne $expectedSimulation) {
        throw "BOM-less UTF-8 simulation QMT path was not preserved."
    }

    [System.IO.File]::WriteAllText(
        $invalidPath,
        '{"value":"\u12"}',
        $utf8NoBom
    )
    $reportedPath = $false
    try {
        $null = Read-ReverseRepoJson -Path $invalidPath
    }
    catch {
        $reportedPath = $_.Exception.Message.Contains($invalidPath)
    }
    if (-not $reportedPath) {
        throw "Malformed JSON error did not identify the source file."
    }

    Write-Output "Windows PowerShell BOM-less UTF-8 JSON test passed."
}
finally {
    if (Test-Path -LiteralPath $validPath -PathType Leaf) {
        Remove-Item -LiteralPath $validPath -Force
    }
    if (Test-Path -LiteralPath $invalidPath -PathType Leaf) {
        Remove-Item -LiteralPath $invalidPath -Force
    }
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        Remove-Item -LiteralPath $testRoot -Force
    }
}
