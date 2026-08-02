[CmdletBinding()]
param([switch]$Check)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$distributionRoot = Join-Path $repoRoot "dist"
$packagePath = Join-Path $distributionRoot "reverse_repo-latest.zip"
$hashPath = Join-Path `
    $distributionRoot `
    "reverse_repo-latest.zip.sha256"

$git = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -eq $git) {
    throw "Maintainer build requires git.exe to enumerate tracked files."
}
$trackedFiles = @(& $git.Source -C $repoRoot ls-files)
if ($LASTEXITCODE -ne 0 -or $trackedFiles.Count -eq 0) {
    throw "Unable to enumerate tracked release files."
}
$trackedFiles = @($trackedFiles | Where-Object {
    $_ -notin @(
        "dist/reverse_repo-latest.zip",
        "dist/reverse_repo-latest.zip.sha256"
    )
})
foreach ($required in @("install.ps1", "rr.cmd", "README.md")) {
    if ($trackedFiles -notcontains $required) {
        throw "Tracked release input is missing: $required"
    }
}

New-Item -ItemType Directory -Force -Path $distributionRoot | Out-Null
if ($Check) {
    if (
        -not (Test-Path -LiteralPath $packagePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $hashPath -PathType Leaf)
    ) {
        throw "Release package is missing. Run build_release_bundle.ps1."
    }
    $expectedHash = (
        (Get-Content -LiteralPath $hashPath -Raw).Trim() -split "\s+"
    )[0].ToLowerInvariant()
    $actualHash = (
        Get-FileHash -LiteralPath $packagePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($expectedHash -ne $actualHash) {
        throw "Release package checksum file does not match the ZIP."
    }

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $checkRoot = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("reverse_repo_bundle_check_" + [guid]::NewGuid().ToString("N"))
    try {
        New-Item -ItemType Directory -Path $checkRoot | Out-Null
        [System.IO.Compression.ZipFile]::ExtractToDirectory(
            $packagePath,
            $checkRoot
        )
        $archiveFiles = @(
            Get-ChildItem -LiteralPath $checkRoot -Recurse -File |
                ForEach-Object {
                    $_.FullName.Substring($checkRoot.Length + 1).Replace("\", "/")
                }
        )
        $difference = @(
            Compare-Object `
                -ReferenceObject @($trackedFiles | Sort-Object) `
                -DifferenceObject @($archiveFiles | Sort-Object)
        )
        if ($difference.Count -ne 0) {
            throw "Release package file list is stale. Rebuild it."
        }
        foreach ($relativePath in $trackedFiles) {
            if ($relativePath -eq "docs/reverse_repo_state_machine_verification.json") {
                continue
            }
            $sourcePath = Join-Path $repoRoot ($relativePath.Replace("/", "\"))
            $archivePath = Join-Path $checkRoot ($relativePath.Replace("/", "\"))
            $sourceHash = (
                Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256
            ).Hash
            $archiveHash = (
                Get-FileHash -LiteralPath $archivePath -Algorithm SHA256
            ).Hash
            if ($sourceHash -ne $archiveHash) {
                throw "Release package content is stale: $relativePath"
            }
        }
    }
    finally {
        if (Test-Path -LiteralPath $checkRoot) {
            Remove-Item -LiteralPath $checkRoot -Recurse -Force
        }
    }
    Write-Output "Release package verification passed: $actualHash"
    exit 0
}

if (Test-Path -LiteralPath $packagePath) {
    Remove-Item -LiteralPath $packagePath -Force
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$stream = [System.IO.File]::Open(
    $packagePath,
    [System.IO.FileMode]::CreateNew
)
try {
    $archive = New-Object System.IO.Compression.ZipArchive(
        $stream,
        [System.IO.Compression.ZipArchiveMode]::Create,
        $false
    )
    try {
        foreach ($relativePath in ($trackedFiles | Sort-Object)) {
            $sourcePath = Join-Path $repoRoot ($relativePath.Replace("/", "\"))
            if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
                throw "Tracked release file is missing: $relativePath"
            }
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive,
                $sourcePath,
                $relativePath,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    }
    finally {
        $archive.Dispose()
    }
}
finally {
    $stream.Dispose()
}

$hash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $hashPath,
    ($hash.ToLowerInvariant() + "  reverse_repo-latest.zip`n"),
    $utf8
)
Write-Output "Release package: $packagePath"
Write-Output "SHA256: $($hash.ToLowerInvariant())"
