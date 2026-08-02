Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$initializerPath = Join-Path `
    $repoRoot `
    "scripts\initialize_reverse_repo.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $initializerPath,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) {
    throw "Initializer cannot be parsed for portable Python tests."
}
foreach ($functionName in @(
    "Assert-PythonExecutable",
    "Install-PortablePython"
)) {
    $definition = $ast.Find(
        {
            param($node)
            $node -is `
                [System.Management.Automation.Language.FunctionDefinitionAst] `
                -and $node.Name -eq $functionName
        },
        $true
    )
    if ($null -eq $definition) {
        throw "$functionName was not found."
    }
    Invoke-Expression $definition.Extent.Text
}

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
    $pythonVersion = "3.12.10"
    $pythonPackageSha512 = (
        "bbda4dcf688a94211b62d50968a91b38f305d0b8d1ecd90269f74a86f8a0a4fc" +
        "ebb7ca162a0753a47691eb3df0c964009bd3d8194c6fd19afae8d5fd01e1cc0f"
    )
    $pythonPackagePath = Join-Path `
        $repoRoot `
        "dist\python-3.12.10-portable.nupkg"
    $runtimeDirectory = Join-Path $testRoot ".runtime\python312"
    $runtimePython = Join-Path $runtimeDirectory "python.exe"
    $bootstrapDirectory = Join-Path $testRoot "tmp\bootstrap"
    $venvDirectory = Join-Path $testRoot ".venv"
    $venvPython = Join-Path $venvDirectory "Scripts\python.exe"

    $userPathBefore = [Environment]::GetEnvironmentVariable("PATH", "User")
    $machinePathBefore = [Environment]::GetEnvironmentVariable(
        "PATH",
        "Machine"
    )
    $registrationBefore = Get-PythonRegistrationSnapshot

    Install-PortablePython
    & $runtimePython -m venv $venvDirectory
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
