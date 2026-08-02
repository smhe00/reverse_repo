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
        # A maintainer checkout exercises the same domestic download that a
        # new installation uses. The pinned hash is the CPython release hash.
        $packagePath = Join-Path $testRoot "python-3.12.10-amd64.zip"
        $packageUri = (
            "https://mirrors.huaweicloud.com/python/3.12.10/" +
            "python-3.12.10-amd64.zip"
        )
        $expectedSize = 32399384
        $expectedHash = (
            "9dc4d0b051bfd5b881f10846ee023fd7" +
            "cea8251871e78b6e8920e5630b15e3bb"
        )
        $downloaded = $false
        $lastError = $null
        foreach ($attempt in 1..3) {
            try {
                Invoke-WebRequest `
                    -UseBasicParsing `
                    -Uri $packageUri `
                    -OutFile $packagePath `
                    -Headers @{ "Cache-Control" = "no-cache" } `
                    -TimeoutSec 300
                if (
                    (Get-Item -LiteralPath $packagePath).Length -ne
                        $expectedSize -or
                    (Get-FileHash `
                        -LiteralPath $packagePath `
                        -Algorithm SHA256
                    ).Hash.ToLowerInvariant() -ne $expectedHash
                ) {
                    throw "Huawei Cloud Python ZIP failed integrity checks."
                }
                $downloaded = $true
                break
            }
            catch {
                $lastError = $_
                if (Test-Path -LiteralPath $packagePath -PathType Leaf) {
                    Remove-Item -LiteralPath $packagePath -Force
                }
                if ($attempt -lt 3) {
                    Start-Sleep -Seconds 2
                }
            }
        }
        if (-not $downloaded) {
            throw "Huawei Cloud Python download failed: $lastError"
        }

        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($packagePath)
        try {
            foreach ($entry in $archive.Entries) {
                $name = $entry.FullName.Replace("\", "/")
                if (
                    [string]::IsNullOrWhiteSpace($name) -or
                    $name.StartsWith("/") -or
                    $name -match "^[A-Za-z]:" -or
                    ($name.Split("/") -contains "..")
                ) {
                    throw "Unsafe path in portable Python package: $name"
                }
            }
            foreach ($requiredEntry in @(
                "python.exe",
                "Lib/venv/__init__.py",
                "Lib/ensurepip/__init__.py",
                "Lib/venv/scripts/nt/venvlauncher.exe",
                "Lib/venv/scripts/nt/venvwlauncher.exe"
            )) {
                if (-not ($archive.Entries.FullName -contains $requiredEntry)) {
                    throw "Portable Python ZIP is missing $requiredEntry."
                }
            }
        }
        finally {
            $archive.Dispose()
        }
        $expandedPath = Join-Path $testRoot "expanded"
        [System.IO.Compression.ZipFile]::ExtractToDirectory(
            $packagePath,
            $expandedPath
        )
        foreach ($launcher in @("venvlauncher.exe", "venvwlauncher.exe")) {
            Copy-Item `
                -LiteralPath (Join-Path `
                    $expandedPath `
                    ("Lib\venv\scripts\nt\" + $launcher)) `
                -Destination (Join-Path $expandedPath $launcher)
        }
        $basePython = Join-Path $expandedPath "python.exe"
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
    $pipProbe = @(& $venvPython -m pip --version 2>&1)
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
