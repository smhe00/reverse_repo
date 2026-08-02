Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot

function Get-PythonRegistrationSnapshot {
    $result = @()
    foreach ($path in @(
        "HKCU:\Software\Python\PythonCore\3.12\InstallPath",
        "HKLM:\Software\Python\PythonCore\3.12\InstallPath",
        "HKLM:\Software\WOW6432Node\Python\PythonCore\3.12\InstallPath"
    )) {
        if (Test-Path -LiteralPath $path) {
            $key = Get-Item -LiteralPath $path
            $result += (
                "$path|" +
                [string]$key.GetValue("") + "|" +
                [string]$key.GetValue("ExecutablePath")
            )
        }
        else {
            $result += "$path|<missing>"
        }
    }
    return ($result -join "`n")
}

$testRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_portable_python_" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $userPathBefore = [Environment]::GetEnvironmentVariable("PATH", "User")
    $machinePathBefore = [Environment]::GetEnvironmentVariable(
        "PATH",
        "Machine"
    )
    $registrationBefore = Get-PythonRegistrationSnapshot

    if (Test-Path -LiteralPath (Join-Path $repoRoot ".git")) {
        # Maintainer checkout: reassemble tracked small parts locally so the
        # test does not depend on Gitee availability.
        $manifestPath = Join-Path `
            $repoRoot `
            "dist\python-3.12.10-portable.parts.json"
        $manifest = Get-Content -LiteralPath $manifestPath -Raw |
            ConvertFrom-Json
        $packagePath = Join-Path $testRoot "python-portable.nupkg"
        $packageStream = [System.IO.File]::Create($packagePath)
        try {
            foreach ($part in @($manifest.parts)) {
                $partPath = Join-Path $repoRoot ("dist\" + [string]$part.name)
                $actualPartHash = (
                    Get-FileHash -LiteralPath $partPath -Algorithm SHA256
                ).Hash.ToLowerInvariant()
                if (
                    (Get-Item -LiteralPath $partPath).Length -ne
                        [long]$part.size -or
                    $actualPartHash -ne [string]$part.sha256
                ) {
                    throw "Portable Python source part is invalid: $partPath"
                }
                $partStream = [System.IO.File]::OpenRead($partPath)
                try {
                    $partStream.CopyTo($packageStream)
                }
                finally {
                    $partStream.Dispose()
                }
            }
        }
        finally {
            $packageStream.Dispose()
        }
        $actualPackageHash = (
            Get-FileHash -LiteralPath $packagePath -Algorithm SHA512
        ).Hash.ToLowerInvariant()
        if ($actualPackageHash -ne [string]$manifest.package_sha512) {
            throw "Reassembled portable Python source package is invalid."
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $expandedPath = Join-Path $testRoot "expanded"
        [System.IO.Compression.ZipFile]::ExtractToDirectory(
            $packagePath,
            $expandedPath
        )
        $basePython = Join-Path $expandedPath "tools\python.exe"
    }
    else {
        # Extracted end-user package: rr init has already placed the runtime
        # inside this project before verify.ps1 is called.
        $basePython = Join-Path `
            $repoRoot `
            ".runtime\python312\python.exe"
    }

    if (-not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
        throw "Portable base Python is missing: $basePython"
    }
    $venvDirectory = Join-Path $testRoot ".venv"
    $venvPython = Join-Path $venvDirectory "Scripts\python.exe"
    & $basePython -m venv $venvDirectory
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Portable Python failed to create a local virtual environment."
    }
    & $venvPython -m pip --version
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "The local virtual environment does not contain pip."
    }

    if (
        $userPathBefore -ne
            [Environment]::GetEnvironmentVariable("PATH", "User") -or
        $machinePathBefore -ne
            [Environment]::GetEnvironmentVariable("PATH", "Machine")
    ) {
        throw "Portable Python changed a persistent PATH value."
    }
    if ($registrationBefore -ne (Get-PythonRegistrationSnapshot)) {
        throw "Portable Python changed Python registration values."
    }
    Write-Output "Portable Python isolation test passed."
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
