[CmdletBinding()]
param(
    [string]$Destination = (Split-Path -Parent $PSScriptRoot),
    [string]$RepositoryBase = (
        "https://gitee.com/smhe/reverse_repo/raw/main"
    ),
    [string]$PackagePath = "",
    [string]$ChecksumPath = "",
    [switch]$SkipPostUpdate,
    [switch]$SkipTaskManagement
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$protectedPathPatterns = @(
    "^\.git(?:/|$)",
    "^\.runtime(?:/|$)",
    "^\.venv(?:/|$)",
    "^reports(?:/|$)",
    "^logs(?:/|$)",
    "^tmp(?:/|$)",
    "^config/.*\.local\."
)
$knownTaskNames = @(
    "miniQMT Reverse Repo First",
    "miniQMT Reverse Repo Second",
    "miniQMT Reverse Repo Once",
    "miniQMT GC001 Daily 90pct 093042",
    "miniQMT GC001 R001 Afternoon Sweep",
    "miniQMT Reverse Repo Morning",
    "miniQMT Reverse Repo Afternoon",
    "miniQMT SIM Interface Stress 5Hz",
    "miniQMT SIM Repo V2 First Recovery",
    "miniQMT SIM Repo V2 Second",
    "miniQMT SIM Repo V2 Certificate",
    "miniQMT SIM Repo V2 Morning Recovery",
    "miniQMT SIM Repo V2 Afternoon",
    "miniQMT LIVE READONLY Morning",
    "miniQMT LIVE READONLY Afternoon",
    "miniQMT Backtest DB Update Yesterday"
)

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

function Convert-ToSafeRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $relative = $Path.Replace("\", "/").TrimStart("/")
    if (
        [string]::IsNullOrWhiteSpace($relative) -or
        $relative -match "^[A-Za-z]:" -or
        ($relative.Split("/") -contains "..")
    ) {
        throw "Unsafe release path: $Path"
    }
    foreach ($pattern in $protectedPathPatterns) {
        if ($relative -match $pattern) {
            throw "Release package targets protected local state: $relative"
        }
    }
    return $relative
}

function Assert-SafeReleaseArchive {
    param([Parameter(Mandatory = $true)][string]$Path)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $files = @()
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace("\", "/")
            if ($name.EndsWith("/")) {
                continue
            }
            $files += Convert-ToSafeRelativePath -Path $name
        }
        foreach ($required in @(
            "rr.cmd",
            "README.md",
            "verify.ps1",
            "scripts/manage_reverse_repo_tasks.ps1",
            "scripts/update_reverse_repo.ps1"
        )) {
            if ($files -notcontains $required) {
                throw "Release package is incomplete: $required is missing."
            }
        }
        if ($files.Count -ne @($files | Sort-Object -Unique).Count) {
            throw "Release package contains duplicate file paths."
        }
        return @($files | Sort-Object)
    }
    finally {
        $archive.Dispose()
    }
}

function Get-InstalledReleaseFiles {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        return @()
    }
    try {
        $manifest = Get-Content `
            -LiteralPath $ManifestPath `
            -Raw `
            -Encoding UTF8 |
            ConvertFrom-Json
        $files = @($manifest.files)
        return @(
            $files |
                ForEach-Object { Convert-ToSafeRelativePath -Path ([string]$_) } |
                Sort-Object -Unique
        )
    }
    catch {
        throw "Installed release manifest is invalid: $ManifestPath - $_"
    }
}

function Save-InstalledReleaseManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Files,
        [Parameter(Mandatory = $true)][string]$PackageSha256
    )
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $payload = [ordered]@{
        schema_version = 1
        updated_at = [datetimeoffset]::Now.ToString("o")
        package_sha256 = $PackageSha256
        files = @($Files | Sort-Object -Unique)
    } | ConvertTo-Json -Depth 5
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $payload + "`n", $utf8)
}

function Assert-AndDisableKnownTasks {
    if ($SkipTaskManagement) {
        return
    }
    $getTask = Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue
    if ($null -eq $getTask) {
        throw "Windows Task Scheduler cmdlets are unavailable."
    }
    $installed = @(
        foreach ($name in $knownTaskNames) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($null -ne $task) {
                $task
            }
        }
    )
    $running = @($installed | Where-Object { [string]$_.State -eq "Running" })
    if ($running.Count -gt 0) {
        throw (
            "A reverse-repo task is running; update refused: " +
            (($running.TaskName | Sort-Object) -join ", ")
        )
    }
    foreach ($task in $installed) {
        if ([string]$task.State -ne "Disabled") {
            Disable-ScheduledTask -TaskName $task.TaskName | Out-Null
        }
    }
    $stillRunning = @(
        foreach ($name in $knownTaskNames) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($null -ne $task -and [string]$task.State -eq "Running") {
                $task
            }
        }
    )
    if ($stillRunning.Count -gt 0) {
        throw (
            "A reverse-repo task started during update preparation; update refused: " +
            (($stillRunning.TaskName | Sort-Object) -join ", ")
        )
    }
}

function Copy-ReleaseFile {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$DestinationRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    $native = $RelativePath.Replace("/", "\")
    $source = Join-Path $SourceRoot $native
    $target = Join-Path $DestinationRoot $native
    $parent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Force
}

$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if (-not (Test-Path -LiteralPath $destinationPath -PathType Container)) {
    throw "Update destination is not a directory: $destinationPath"
}
if (Test-Path -LiteralPath (Join-Path $destinationPath ".git")) {
    throw "rr up is for no-Git installations. This directory has .git; use git pull."
}
foreach ($requiredLocal in @("rr.cmd", "verify.ps1", "scripts")) {
    if (-not (Test-Path -LiteralPath (Join-Path $destinationPath $requiredLocal))) {
        throw "This is not an initialized reverse_repo directory: $requiredLocal missing."
    }
}
if (($PackagePath -eq "") -ne ($ChecksumPath -eq "")) {
    throw "PackagePath and ChecksumPath must be supplied together."
}

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_update_" + [guid]::NewGuid().ToString("N"))
$downloadedPackage = Join-Path $temporaryRoot "reverse_repo-latest.zip"
$downloadedHash = Join-Path $temporaryRoot "reverse_repo-latest.zip.sha256"
$stagingPath = Join-Path $temporaryRoot "expanded"
$backupPath = Join-Path $temporaryRoot "backup"
$manifestPath = Join-Path `
    $destinationPath `
    "config\release_files.local.json"
$liveManifestPath = Join-Path `
    $destinationPath `
    "config\repo_live_enable_manifest.local.json"
$affectedFiles = @()
$newlyCreatedFiles = @()
$updateStarted = $false

try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    if ($PackagePath -ne "") {
        Copy-Item -LiteralPath $PackagePath -Destination $downloadedPackage
        Copy-Item -LiteralPath $ChecksumPath -Destination $downloadedHash
    }
    else {
        $base = $RepositoryBase.TrimEnd("/")
        Write-Output "Downloading verified reverse_repo release package..."
        Get-RemoteFile `
            -Uri "$base/dist/reverse_repo-latest.zip" `
            -Path $downloadedPackage
        Get-RemoteFile `
            -Uri "$base/dist/reverse_repo-latest.zip.sha256" `
            -Path $downloadedHash
    }

    $expectedHash = (
        (Get-Content -LiteralPath $downloadedHash -Raw).Trim() -split "\s+"
    )[0].ToLowerInvariant()
    if ($expectedHash -notmatch "^[0-9a-f]{64}$") {
        throw "The release checksum file is invalid."
    }
    $actualHash = (
        Get-FileHash -LiteralPath $downloadedPackage -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Release package checksum mismatch."
    }

    $releaseFiles = Assert-SafeReleaseArchive -Path $downloadedPackage
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory(
        $downloadedPackage,
        $stagingPath
    )
    Assert-AndDisableKnownTasks
    if (Test-Path -LiteralPath $liveManifestPath -PathType Leaf) {
        Remove-Item -LiteralPath $liveManifestPath -Force
    }

    $oldReleaseFiles = Get-InstalledReleaseFiles -ManifestPath $manifestPath
    $staleFiles = @(
        $oldReleaseFiles |
            Where-Object { $releaseFiles -notcontains $_ }
    )
    $affectedFiles = @(
        $releaseFiles + $staleFiles |
            Sort-Object -Unique
    )
    New-Item -ItemType Directory -Path $backupPath | Out-Null
    foreach ($relative in $affectedFiles) {
        $native = $relative.Replace("/", "\")
        $target = Join-Path $destinationPath $native
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Copy-ReleaseFile `
                -SourceRoot $destinationPath `
                -DestinationRoot $backupPath `
                -RelativePath $relative
        }
        else {
            $newlyCreatedFiles += $relative
        }
    }

    $updateStarted = $true
    foreach ($relative in $releaseFiles) {
        Copy-ReleaseFile `
            -SourceRoot $stagingPath `
            -DestinationRoot $destinationPath `
            -RelativePath $relative
    }
    foreach ($relative in $staleFiles) {
        $target = Join-Path $destinationPath ($relative.Replace("/", "\"))
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Remove-Item -LiteralPath $target -Force
        }
    }

    if (-not $SkipPostUpdate) {
        Write-Output "Running local verification after update..."
        & (Join-Path $destinationPath "verify.ps1")
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "Post-update verification failed."
        }
        Write-Output "Reinstalling live tasks in Disabled state..."
        & (Join-Path $destinationPath "rr.cmd") add
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "Post-update task installation failed."
        }
    }

    Save-InstalledReleaseManifest `
        -Path $manifestPath `
        -Files $releaseFiles `
        -PackageSha256 $actualHash
    Write-Output "reverse_repo update completed: $actualHash"
    Write-Output "Local configuration, bindings, certificates and reports were preserved."
    if (-not $SkipPostUpdate) {
        Write-Output "Tasks are Disabled. Review .\rr stat, then run .\rr on explicitly."
    }
}
catch {
    $failure = $_
    if ($updateStarted) {
        Write-Warning "Update failed; restoring previous program files."
        foreach ($relative in $affectedFiles) {
            $native = $relative.Replace("/", "\")
            $backup = Join-Path $backupPath $native
            $target = Join-Path $destinationPath $native
            if (Test-Path -LiteralPath $backup -PathType Leaf) {
                $parent = Split-Path -Parent $target
                New-Item -ItemType Directory -Force -Path $parent | Out-Null
                Copy-Item -LiteralPath $backup -Destination $target -Force
            }
            elseif ($newlyCreatedFiles -contains $relative) {
                if (Test-Path -LiteralPath $target -PathType Leaf) {
                    Remove-Item -LiteralPath $target -Force
                }
            }
        }
    }
    throw $failure
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
