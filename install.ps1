[CmdletBinding()]
param(
    [string]$Destination = (Get-Location).Path,
    [string]$RepositoryBase = (
        "https://gitee.com/smhe/reverse_repo/raw/main"
    ),
    [switch]$SkipInitialize
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-RemoteFile {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $lastError = $null
    foreach ($attempt in 1..3) {
        try {
            Invoke-WebRequest `
                -Uri $Uri `
                -OutFile $Path `
                -UseBasicParsing `
                -Headers @{ "Cache-Control" = "no-cache" } `
                -TimeoutSec 300
            return
        }
        catch {
            $lastError = $_
            if ($attempt -lt 3) {
                Start-Sleep -Seconds 2
            }
        }
    }
    throw "Download failed after three attempts: $Uri - $lastError"
}

function Assert-SafeArchive {
    param([Parameter(Mandatory = $true)][string]$Path)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace("\", "/")
            if (
                [string]::IsNullOrWhiteSpace($name) -or
                $name.StartsWith("/") -or
                $name -match "^[A-Za-z]:" -or
                ($name.Split("/") -contains "..")
            ) {
                throw "Unsafe path in release package: $name"
            }
        }
        foreach ($required in @(
            "rr.cmd",
            "README.md",
            "scripts/initialize_reverse_repo.ps1",
            "scripts/manage_reverse_repo_tasks.ps1"
        )) {
            if (-not ($archive.Entries.FullName -contains $required)) {
                throw "Release package is incomplete: $required is missing."
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if (-not (Test-Path -LiteralPath $destinationPath)) {
    New-Item -ItemType Directory -Path $destinationPath | Out-Null
}
if (-not (Test-Path -LiteralPath $destinationPath -PathType Container)) {
    throw "Destination is not a directory: $destinationPath"
}
$existingItems = @(Get-ChildItem -LiteralPath $destinationPath -Force)
if ($existingItems.Count -ne 0) {
    throw (
        "Destination must be an empty directory. Nothing was changed: " +
        $destinationPath
    )
}

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_install_" + [guid]::NewGuid().ToString("N"))
$packagePath = Join-Path $temporaryRoot "reverse_repo-latest.zip"
$hashPath = Join-Path $temporaryRoot "reverse_repo-latest.zip.sha256"
$stagingPath = Join-Path $temporaryRoot "expanded"

try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    $base = $RepositoryBase.TrimEnd("/")
    Write-Output "Downloading reverse_repo release package from Gitee..."
    Get-RemoteFile `
        -Uri "$base/dist/reverse_repo-latest.zip" `
        -Path $packagePath
    Get-RemoteFile `
        -Uri "$base/dist/reverse_repo-latest.zip.sha256" `
        -Path $hashPath

    $expectedHash = (
        (Get-Content -LiteralPath $hashPath -Raw).Trim() -split "\s+"
    )[0].ToLowerInvariant()
    if ($expectedHash -notmatch "^[0-9a-f]{64}$") {
        throw "The release checksum file is invalid."
    }
    $actualHash = (
        Get-FileHash -LiteralPath $packagePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw (
            "Release package checksum mismatch. Expected $expectedHash, " +
            "got $actualHash."
        )
    }

    Assert-SafeArchive -Path $packagePath
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory(
        $packagePath,
        $stagingPath
    )

    if (@(Get-ChildItem -LiteralPath $destinationPath -Force).Count -ne 0) {
        throw "Destination changed during download; installation was cancelled."
    }
    Get-ChildItem -LiteralPath $stagingPath -Force |
        Copy-Item -Destination $destinationPath -Recurse
    $releaseFiles = @(
        Get-ChildItem -LiteralPath $stagingPath -Recurse -File |
            ForEach-Object {
                $_.FullName.Substring($stagingPath.Length + 1).Replace("\", "/")
            } |
            Sort-Object -Unique
    )
    $releaseManifestPath = Join-Path `
        $destinationPath `
        "config\release_files.local.json"
    $releaseManifest = [ordered]@{
        schema_version = 1
        updated_at = [datetimeoffset]::Now.ToString("o")
        package_sha256 = $actualHash
        files = $releaseFiles
    } | ConvertTo-Json -Depth 5
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $releaseManifestPath,
        $releaseManifest + "`n",
        $utf8
    )
    Write-Output "Package checksum verified: $actualHash"
    Write-Output "reverse_repo installed in: $destinationPath"
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

if ($SkipInitialize) {
    Write-Output "Initialization skipped by request. Run .\rr init later."
    exit 0
}

Write-Output "Starting rr init..."
Push-Location -LiteralPath $destinationPath
try {
    & (Join-Path $destinationPath "rr.cmd") init
    if ($null -ne $LASTEXITCODE -and [int]$LASTEXITCODE -ne 0) {
        throw "rr init failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
