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
    throw "Initializer cannot be parsed for QMT path tests."
}
$resolver = $ast.Find(
    {
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] `
            -and $node.Name -eq "Resolve-QmtUserdataPath"
    },
    $true
)
if ($null -eq $resolver) {
    throw "Resolve-QmtUserdataPath was not found."
}
Invoke-Expression $resolver.Extent.Text
$basePythonFinder = $ast.Find(
    {
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] `
            -and $node.Name -eq "Find-CompatibleBasePython"
    },
    $true
)
if ($null -eq $basePythonFinder) {
    throw "Find-CompatibleBasePython was not found."
}
Invoke-Expression $basePythonFinder.Extent.Text

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Actual,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if ($Expected -ne $Actual) {
        throw "$Message Expected=$Expected Actual=$Actual"
    }
}

$testRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_qmt_path_test_" + [guid]::NewGuid().ToString("N"))
try {
    $liveRoot = Join-Path $testRoot "LiveQMT"
    $simulationRoot = Join-Path $testRoot "模拟QMT"
    $liveUserdata = Join-Path $liveRoot "userdata_mini"
    $simulationUserdata = Join-Path $simulationRoot "userdata_mini"
    New-Item -ItemType Directory -Path $liveUserdata | Out-Null
    New-Item -ItemType Directory -Path $simulationRoot | Out-Null

    # Existing userdata configuration is displayed as its parent install root.
    $script:readMode = "existing"
    $script:readCount = 0
    function Read-Host {
        param([string]$Prompt)
        $script:readCount += 1
        if ($script:readMode -eq "existing") {
            return ""
        }
        if ($script:readMode -eq "wait") {
            if ($script:readCount -eq 1) {
                return $simulationRoot
            }
            New-Item -ItemType Directory -Path $simulationUserdata | Out-Null
            return "Y"
        }
        if ($script:readMode -eq "swap") {
            return $simulationRoot
        }
        return $liveRoot
    }
    $resolvedLive = Resolve-QmtUserdataPath `
        -Prompt "live" `
        -Environment "live" `
        -DefaultInstallRoot "C:\unused" `
        -Existing $liveUserdata
    Assert-Equal `
        -Expected $liveUserdata `
        -Actual $resolvedLive `
        -Message "Existing userdata path was not preserved."

    # Missing userdata pauses; after the mocked independent-trading login
    # creates it, the same resolver call continues successfully.
    $script:readMode = "wait"
    $script:readCount = 0
    $resolvedSimulation = Resolve-QmtUserdataPath `
        -Prompt "simulation" `
        -Environment "simulation" `
        -DefaultInstallRoot $simulationRoot
    Assert-Equal `
        -Expected $simulationUserdata `
        -Actual $resolvedSimulation `
        -Message "Resolver did not continue after userdata appeared."
    Assert-Equal `
        -Expected "2" `
        -Actual ([string]$script:readCount) `
        -Message "Resolver did not wait exactly once for login."

    # An obviously swapped live path is rejected before any wait loop.
    $script:readMode = "swap"
    $script:readCount = 0
    $swapRejected = $false
    try {
        $null = Resolve-QmtUserdataPath `
            -Prompt "live" `
            -Environment "live" `
            -DefaultInstallRoot $liveRoot `
            -Existing ""
    }
    catch {
        $swapRejected = $_.Exception.Message -match "两个路径可能填反"
    }
    if (-not $swapRejected) {
        throw "An obviously swapped live QMT path was not rejected."
    }

    # Python Manager's stable per-user runtime is preferred over downloading
    # another interpreter into a directory the user may later clear.
    $oldLocalAppData = $env:LOCALAPPDATA
    try {
        $env:LOCALAPPDATA = $testRoot
        $runtimePython = Join-Path $testRoot "missing\python.exe"
        $expectedManagerPython = Join-Path `
            $testRoot `
            "Python\pythoncore-3.12-64\python.exe"
        function Test-CompatibleBasePython {
            param([string]$PythonPath)
            return $PythonPath -eq $expectedManagerPython
        }
        $selectedPython = Find-CompatibleBasePython
        Assert-Equal `
            -Expected $expectedManagerPython `
            -Actual $selectedPython `
            -Message "Python Manager runtime was not selected."
    }
    finally {
        $env:LOCALAPPDATA = $oldLocalAppData
    }

    Write-Output "QMT path and base-Python selection tests passed."
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
