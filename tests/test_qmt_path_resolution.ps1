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
$finder = $ast.Find(
    {
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] `
            -and $node.Name -eq "Get-RunningMiniQmtInstallRoot"
    },
    $true
)
if ($null -eq $finder) {
    throw "Get-RunningMiniQmtInstallRoot was not found."
}
Invoke-Expression $finder.Extent.Text
Invoke-Expression $resolver.Extent.Text

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Expected,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Actual,
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
    $otherLiveRoot = Join-Path $testRoot "OtherLiveQMT"
    $liveUserdata = Join-Path $liveRoot "userdata_mini"
    $simulationUserdata = Join-Path $simulationRoot "userdata_mini"
    New-Item -ItemType Directory -Path $liveUserdata | Out-Null
    New-Item -ItemType Directory -Path $simulationRoot | Out-Null
    New-Item -ItemType Directory -Path $otherLiveRoot | Out-Null

    $script:processMode = "unique"
    function Get-CimInstance {
        param(
            [string]$ClassName,
            [string]$Filter,
            [object]$ErrorAction
        )
        $result = @(
            [pscustomobject]@{
                ExecutablePath = Join-Path `
                    $liveRoot `
                    "bin.x64\XtMiniQmt.exe"
            },
            [pscustomobject]@{
                ExecutablePath = Join-Path `
                    $simulationRoot `
                    "bin.x64\XtMiniQmt.exe"
            }
        )
        if ($script:processMode -eq "ambiguous") {
            $result += [pscustomobject]@{
                ExecutablePath = Join-Path `
                    $otherLiveRoot `
                    "bin.x64\XtMiniQmt.exe"
            }
        }
        return $result
    }
    $detectedLive = Get-RunningMiniQmtInstallRoot `
        -Environment "live" `
        3>$null `
        6>$null
    $detectedSimulation = Get-RunningMiniQmtInstallRoot `
        -Environment "simulation" `
        3>$null `
        6>$null
    Assert-Equal `
        -Expected $liveRoot `
        -Actual $detectedLive `
        -Message "Running live miniQMT directory was not discovered."
    Assert-Equal `
        -Expected $simulationRoot `
        -Actual $detectedSimulation `
        -Message "Running simulation miniQMT directory was not discovered."
    $script:processMode = "ambiguous"
    $ambiguousLive = Get-RunningMiniQmtInstallRoot `
        -Environment "live" `
        3>$null `
        6>$null
    Assert-Equal `
        -Expected "" `
        -Actual $ambiguousLive `
        -Message "Ambiguous running miniQMT paths must not be selected."
    $script:processMode = "unique"

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
        if ($script:readMode -eq "detected") {
            return ""
        }
        return $liveRoot
    }
    $resolvedLive = Resolve-QmtUserdataPath `
        -Prompt "live" `
        -Environment "live" `
        -DefaultInstallRoot "C:\unused" `
        -DetectedInstallRoot "C:\also-unused" `
        -Existing $liveUserdata `
        3>$null `
        6>$null
    Assert-Equal `
        -Expected $liveUserdata `
        -Actual $resolvedLive `
        -Message "Existing userdata path was not preserved."

    # A uniquely discovered running process path becomes the prompt default.
    $script:readMode = "detected"
    $script:readCount = 0
    $resolvedDetected = Resolve-QmtUserdataPath `
        -Prompt "live" `
        -Environment "live" `
        -DefaultInstallRoot "C:\unused" `
        -DetectedInstallRoot $detectedLive `
        -Existing "" `
        3>$null `
        6>$null
    Assert-Equal `
        -Expected $liveUserdata `
        -Actual $resolvedDetected `
        -Message "Detected process path was not injected as the default."

    # Missing userdata pauses; after the mocked independent-trading login
    # creates it, the same resolver call continues successfully.
    $script:readMode = "wait"
    $script:readCount = 0
    $resolvedSimulation = Resolve-QmtUserdataPath `
        -Prompt "simulation" `
        -Environment "simulation" `
        -DefaultInstallRoot $simulationRoot `
        3>$null `
        6>$null
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

    Write-Output (
        "QMT install-root discovery and independent-login wait tests passed."
    )
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
