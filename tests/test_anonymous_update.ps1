[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$updater = Join-Path $repoRoot "scripts\update_reverse_repo.ps1"
$windowsPowerShell = Join-Path `
    $env:SystemRoot `
    "System32\WindowsPowerShell\v1.0\powershell.exe"
$testRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_update_test_" + [guid]::NewGuid().ToString("N"))

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Write-Utf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $utf8)
}

function New-TestDestination {
    param([Parameter(Mandatory = $true)][string]$Path)
    New-Item -ItemType Directory -Path $Path | Out-Null
    Write-Utf8 (Join-Path $Path "rr.cmd") "old rr"
    Write-Utf8 (Join-Path $Path "verify.ps1") "old verify"
    Write-Utf8 `
        (Join-Path $Path "scripts\manage_reverse_repo_tasks.ps1") `
        "old manager"
    Write-Utf8 (Join-Path $Path "obsolete.txt") "obsolete"
    Write-Utf8 `
        (Join-Path $Path "config\runtime.local.json") `
        '{"local":"keep"}'
    Write-Utf8 `
        (Join-Path $Path "config\repo_live_enable_manifest.local.json") `
        "must be revoked"
    Write-Utf8 (Join-Path $Path "reports\evidence.txt") "keep report"
    Write-Utf8 (Join-Path $Path ".venv\keep.txt") "keep venv"
    $manifest = [ordered]@{
        schema_version = 1
        files = @(
            "obsolete.txt",
            "rr.cmd",
            "scripts/manage_reverse_repo_tasks.ps1",
            "verify.ps1"
        )
    } | ConvertTo-Json -Depth 4
    Write-Utf8 `
        (Join-Path $Path "config\release_files.local.json") `
        $manifest
}

function New-TestPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$VerifyText,
        [switch]$IncludeProtectedPath
    )
    $source = Join-Path $Root "source"
    $zip = Join-Path $Root "release.zip"
    $sha = Join-Path $Root "release.zip.sha256"
    New-Item -ItemType Directory -Path $source | Out-Null
    Write-Utf8 (Join-Path $source "rr.cmd") "new rr"
    Write-Utf8 (Join-Path $source "README.md") "new readme"
    Write-Utf8 (Join-Path $source "verify.ps1") $VerifyText
    Write-Utf8 `
        (Join-Path $source "scripts\manage_reverse_repo_tasks.ps1") `
        "new manager"
    Copy-Item `
        -LiteralPath $updater `
        -Destination (Join-Path $source "scripts\update_reverse_repo.ps1")
    Write-Utf8 (Join-Path $source "new.txt") "new file"
    if ($IncludeProtectedPath) {
        Write-Utf8 (Join-Path $source "reports\overwrite.txt") "unsafe"
    }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory($source, $zip)
    $digest = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Utf8 $sha ($digest + "`n")
    return [pscustomobject]@{
        Zip = $zip
        Sha = $sha
        Digest = $digest
    }
}

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null

    $successRoot = Join-Path $testRoot "success"
    $successDestination = Join-Path $successRoot "destination"
    New-Item -ItemType Directory -Path $successRoot | Out-Null
    New-TestDestination $successDestination
    $successPackage = New-TestPackage `
        -Root (Join-Path $successRoot "package") `
        -VerifyText "Write-Output 'not executed'"
    $savedErrorPreference = $ErrorActionPreference
    & $windowsPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $updater `
        -Destination $successDestination `
        -PackagePath $successPackage.Zip `
        -ChecksumPath $successPackage.Sha `
        -SkipPostUpdate `
        -SkipTaskManagement
    Assert-True ($LASTEXITCODE -eq 0) "Successful anonymous update failed."
    Assert-True `
        ((Get-Content (Join-Path $successDestination "rr.cmd") -Raw) -eq "new rr") `
        "Tracked file was not updated."
    Assert-True `
        (Test-Path (Join-Path $successDestination "new.txt")) `
        "New release file was not installed."
    Assert-True `
        (-not (Test-Path (Join-Path $successDestination "obsolete.txt"))) `
        "Stale tracked file was not removed."
    Assert-True `
        ((Get-Content (Join-Path $successDestination "config\runtime.local.json") -Raw) -eq '{"local":"keep"}') `
        "Local runtime configuration changed."
    Assert-True `
        ((Get-Content (Join-Path $successDestination "reports\evidence.txt") -Raw) -eq "keep report") `
        "Report evidence changed."
    Assert-True `
        ((Get-Content (Join-Path $successDestination ".venv\keep.txt") -Raw) -eq "keep venv") `
        "Virtual environment changed."
    Assert-True `
        (-not (Test-Path (Join-Path $successDestination "config\repo_live_enable_manifest.local.json"))) `
        "Live-enable snapshot was not revoked."
    $savedManifest = Get-Content `
        (Join-Path $successDestination "config\release_files.local.json") `
        -Raw |
        ConvertFrom-Json
    Assert-True `
        ($savedManifest.package_sha256 -eq $successPackage.Digest) `
        "Installed release manifest has the wrong hash."

    $protectedRoot = Join-Path $testRoot "protected"
    $protectedDestination = Join-Path $protectedRoot "destination"
    New-Item -ItemType Directory -Path $protectedRoot | Out-Null
    New-TestDestination $protectedDestination
    $protectedPackage = New-TestPackage `
        -Root (Join-Path $protectedRoot "package") `
        -VerifyText "Write-Output 'not executed'" `
        -IncludeProtectedPath
    $ErrorActionPreference = "Continue"
    & $windowsPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $updater `
        -Destination $protectedDestination `
        -PackagePath $protectedPackage.Zip `
        -ChecksumPath $protectedPackage.Sha `
        -SkipPostUpdate `
        -SkipTaskManagement *> $null
    $protectedExitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedErrorPreference
    Assert-True ($protectedExitCode -ne 0) "Protected-path package was accepted."
    Assert-True `
        ((Get-Content (Join-Path $protectedDestination "rr.cmd") -Raw) -eq "old rr") `
        "Rejected package changed program files."
    Assert-True `
        ((Get-Content (Join-Path $protectedDestination "reports\evidence.txt") -Raw) -eq "keep report") `
        "Rejected package changed reports."

    $rollbackRoot = Join-Path $testRoot "rollback"
    $rollbackDestination = Join-Path $rollbackRoot "destination"
    New-Item -ItemType Directory -Path $rollbackRoot | Out-Null
    New-TestDestination $rollbackDestination
    $rollbackPackage = New-TestPackage `
        -Root (Join-Path $rollbackRoot "package") `
        -VerifyText "throw 'intentional post-update failure'"
    $ErrorActionPreference = "Continue"
    & $windowsPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $updater `
        -Destination $rollbackDestination `
        -PackagePath $rollbackPackage.Zip `
        -ChecksumPath $rollbackPackage.Sha `
        -SkipTaskManagement *> $null
    $rollbackExitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedErrorPreference
    Assert-True ($rollbackExitCode -ne 0) "Post-update failure did not fail."
    Assert-True `
        ((Get-Content (Join-Path $rollbackDestination "rr.cmd") -Raw) -eq "old rr") `
        "Rollback did not restore an overwritten file."
    Assert-True `
        (Test-Path (Join-Path $rollbackDestination "obsolete.txt")) `
        "Rollback did not restore a stale file."
    Assert-True `
        (-not (Test-Path (Join-Path $rollbackDestination "new.txt"))) `
        "Rollback did not remove a newly introduced file."
    Assert-True `
        ((Get-Content (Join-Path $rollbackDestination "config\runtime.local.json") -Raw) -eq '{"local":"keep"}') `
        "Rollback changed local configuration."

    Write-Output "Anonymous no-Git update and rollback tests passed."
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
