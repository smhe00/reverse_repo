[CmdletBinding()]
param(
    [switch]$SkipAccountBinding,
    [switch]$SkipTaskInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$windowsPowerShell = Get-ReverseRepoPowerShell
$pythonVersion = "3.12.10"
$pythonSha256 = (
    "67b5635e80ea51072b87941312d00ec8" +
    "927c4db9ba18938f7ad2d27b328b95fb"
)
$pythonFilename = "python-$pythonVersion-amd64.exe"
$runtimeDirectory = Join-Path $repoRoot ".runtime\python312"
$runtimePython = Join-Path $runtimeDirectory "python.exe"
$script:BasePythonPath = ""
$venvDirectory = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$runtimeConfigPath = Join-Path $repoRoot "config\runtime.local.json"
$requirementsPath = Join-Path $repoRoot "requirements.txt"
$bootstrapDirectory = Join-Path $repoRoot "tmp\bootstrap"
$installerPath = Join-Path $bootstrapDirectory $pythonFilename
$signingKeyPath = Join-Path `
    $repoRoot `
    "config\repo_release_gate_secret.local.json"

$pythonMirrors = @(
    [pscustomobject]@{
        Name = "清华大学TUNA"
        Uri = (
            "https://mirrors.tuna.tsinghua.edu.cn/python/" +
            "$pythonVersion/$pythonFilename"
        )
    },
    [pscustomobject]@{
        Name = "北京外国语大学BFSU"
        Uri = (
            "https://mirrors.bfsu.edu.cn/python/" +
            "$pythonVersion/$pythonFilename"
        )
    },
    [pscustomobject]@{
        Name = "华为云"
        Uri = (
            "https://mirrors.huaweicloud.com/python/" +
            "$pythonVersion/$pythonFilename"
        )
    }
)
$pipMirrors = @(
    [pscustomobject]@{
        Name = "清华大学TUNA"
        Index = "https://pypi.tuna.tsinghua.edu.cn/simple"
        Probe = "https://pypi.tuna.tsinghua.edu.cn/simple/pip/"
    },
    [pscustomobject]@{
        Name = "北京外国语大学BFSU"
        Index = "https://mirrors.bfsu.edu.cn/pypi/web/simple"
        Probe = "https://mirrors.bfsu.edu.cn/pypi/web/simple/pip/"
    },
    [pscustomobject]@{
        Name = "华为云"
        Index = "https://repo.huaweicloud.com/repository/pypi/simple"
        Probe = (
            "https://repo.huaweicloud.com/repository/pypi/simple/pip/"
        )
    }
)

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $Path,
        $Text + [Environment]::NewLine,
        $encoding
    )
}

function Assert-LiveTasksInactive {
    foreach ($name in @(
        "miniQMT Reverse Repo First",
        "miniQMT Reverse Repo Second"
    )) {
        $task = Get-ScheduledTask `
            -TaskName $name `
            -ErrorAction SilentlyContinue
        if ($null -ne $task -and [string]$task.State -ne "Disabled") {
            throw (
                "Initialization requires disabled live tasks. Run .\rr off " +
                "first: $name is $($task.State)."
            )
        }
    }
}

function Get-ReachableMirrors {
    param(
        [Parameter(Mandatory = $true)][object[]]$Mirrors,
        [Parameter(Mandatory = $true)][string]$ProbeProperty
    )
    $reachable = @()
    foreach ($mirror in $Mirrors) {
        $probeUri = [string]$mirror.$ProbeProperty
        $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $response = Invoke-WebRequest `
                -UseBasicParsing `
                -Method Head `
                -Uri $probeUri `
                -TimeoutSec 10
            $stopwatch.Stop()
            if ([int]$response.StatusCode -ge 200 -and `
                [int]$response.StatusCode -lt 400) {
                $reachable += [pscustomobject]@{
                    Mirror = $mirror
                    LatencyMs = $stopwatch.ElapsedMilliseconds
                }
            }
        }
        catch {
            $stopwatch.Stop()
            Write-Warning (
                "镜像探测失败：$($mirror.Name) - " +
                $_.Exception.Message
            )
        }
    }
    return @(
        $reachable |
            Sort-Object -Property LatencyMs |
            ForEach-Object { $_.Mirror }
    )
}

function Assert-PythonExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [switch]$RequireXtQuant
    )
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Python executable is missing: $PythonPath"
    }
    $probe = if ($RequireXtQuant) {
        (
            "import struct,sys; " +
            "from xtquant import xtconstant,xtdata,xttype; " +
            "from xtquant.xttrader import XtQuantTrader; " +
            "print(sys.version_info[:3],struct.calcsize('P')*8)"
        )
    }
    else {
        (
            "import struct,sys; " +
            "assert sys.version_info[:3] == (3,12,10); " +
            "assert struct.calcsize('P')*8 == 64; " +
            "print(sys.version.split()[0])"
        )
    }
    & $PythonPath -c $probe
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Python runtime probe failed: $PythonPath"
    }
}

function Test-CompatibleBasePython {
    param([Parameter(Mandatory = $true)][string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return $false
    }
    $probe = & $PythonPath -c (
        "import struct,sys;" +
        "print('.'.join(map(str,sys.version_info[:3]))+'|'+" +
        "str(struct.calcsize('P')*8))"
    ) 2>$null
    return (
        $null -ne $LASTEXITCODE `
        -and [int]$LASTEXITCODE -eq 0 `
        -and [string]$probe -eq "3.12.10|64"
    )
}

function Find-CompatibleBasePython {
    $candidates = @(
        $runtimePython,
        (Join-Path `
            $env:LOCALAPPDATA `
            "Python\pythoncore-3.12-64\python.exe"),
        (Join-Path `
            $env:LOCALAPPDATA `
            "Programs\Python\Python312\python.exe")
    )
    foreach ($registryPath in @(
        "HKCU:\Software\Python\PythonCore\3.12\InstallPath",
        "HKLM:\Software\Python\PythonCore\3.12\InstallPath",
        "HKLM:\Software\WOW6432Node\Python\PythonCore\3.12\InstallPath"
    )) {
        if (-not (Test-Path -LiteralPath $registryPath)) {
            continue
        }
        $key = Get-Item -LiteralPath $registryPath
        $registeredExecutable = [string]$key.GetValue("ExecutablePath")
        $registeredRoot = [string]$key.GetValue("")
        if (-not [string]::IsNullOrWhiteSpace($registeredExecutable)) {
            $candidates += $registeredExecutable
        }
        if (-not [string]::IsNullOrWhiteSpace($registeredRoot)) {
            $candidates += Join-Path $registeredRoot "python.exe"
        }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-CompatibleBasePython -PythonPath $candidate) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Install-PrivatePython {
    $compatiblePython = Find-CompatibleBasePython
    if (-not [string]::IsNullOrWhiteSpace($compatiblePython)) {
        $script:BasePythonPath = $compatiblePython
        Write-Output "复用已验证的Python 3.12.10 x64：$compatiblePython"
        return
    }
    if (
        (Test-Path -LiteralPath $runtimeDirectory -PathType Container) `
        -and (Get-ChildItem -LiteralPath $runtimeDirectory -Force).Count -gt 0
    ) {
        throw (
            "Private Python directory is non-empty but incomplete: " +
            "$runtimeDirectory. Move it aside before retrying."
        )
    }
    New-Item -ItemType Directory -Force -Path $bootstrapDirectory |
        Out-Null
    $orderedMirrors = Get-ReachableMirrors `
        -Mirrors $pythonMirrors `
        -ProbeProperty "Uri"
    if ($orderedMirrors.Count -eq 0) {
        throw "No domestic Python download mirror is reachable."
    }
    $downloaded = $false
    foreach ($mirror in $orderedMirrors) {
        $partialPath = "$installerPath.partial"
        if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
            Remove-Item -LiteralPath $partialPath -Force
        }
        Write-Output "从$($mirror.Name)下载Python $pythonVersion x64。"
        try {
            Invoke-WebRequest `
                -UseBasicParsing `
                -Uri $mirror.Uri `
                -OutFile $partialPath `
                -TimeoutSec 300
            $actualHash = (
                Get-FileHash -LiteralPath $partialPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            if ($actualHash -ne $pythonSha256) {
                throw "Python installer SHA-256 mismatch."
            }
            $signature = Get-AuthenticodeSignature -FilePath $partialPath
            if (
                [string]$signature.Status -ne "Valid" `
                -or $null -eq $signature.SignerCertificate `
                -or [string]$signature.SignerCertificate.Subject `
                    -notmatch "Python Software Foundation"
            ) {
                throw "Python installer Authenticode signature is invalid."
            }
            Move-Item `
                -LiteralPath $partialPath `
                -Destination $installerPath `
                -Force
            $downloaded = $true
            break
        }
        catch {
            Write-Warning (
                "Python下载或校验失败：$($mirror.Name) - " +
                $_.Exception.Message
            )
        }
        finally {
            if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
                Remove-Item -LiteralPath $partialPath -Force
            }
        }
    }
    if (-not $downloaded) {
        throw "All domestic Python mirrors failed download or verification."
    }
    $arguments = @(
        "/quiet",
        "InstallAllUsers=0",
        "TargetDir=`"$runtimeDirectory`"",
        "PrependPath=0",
        "Include_pip=1",
        "Include_launcher=0",
        "Include_test=0",
        "Include_doc=0",
        "Shortcuts=0",
        "AssociateFiles=0"
    )
    $process = Start-Process `
        -FilePath $installerPath `
        -ArgumentList $arguments `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ([int]$process.ExitCode -ne 0) {
        throw "Private Python installer failed: $($process.ExitCode)"
    }
    $compatiblePython = Find-CompatibleBasePython
    if ([string]::IsNullOrWhiteSpace($compatiblePython)) {
        Write-Warning (
            "Python安装器返回成功，但登记的解释器文件不存在；" +
            "尝试修复Windows中的既有Python 3.12.10安装。"
        )
        $repair = Start-Process `
            -FilePath $installerPath `
            -ArgumentList @("/repair", "/quiet") `
            -Wait `
            -PassThru `
            -WindowStyle Hidden
        if ([int]$repair.ExitCode -ne 0) {
            throw "Python repair failed: $($repair.ExitCode)"
        }
        $compatiblePython = Find-CompatibleBasePython
    }
    if ([string]::IsNullOrWhiteSpace($compatiblePython)) {
        throw (
            "Python installer returned success but no compatible executable " +
            "was found. Remove the stale Python 3.12.10 entry from Windows " +
            "Installed apps, then run .\rr init again."
        )
    }
    $script:BasePythonPath = $compatiblePython
    Assert-PythonExecutable -PythonPath $script:BasePythonPath
}

function Install-VirtualEnvironment {
    if ([string]::IsNullOrWhiteSpace($script:BasePythonPath)) {
        throw "A compatible base Python was not selected."
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        if (
            (Test-Path -LiteralPath $venvDirectory -PathType Container) `
            -and (Get-ChildItem -LiteralPath $venvDirectory -Force).Count -gt 0
        ) {
            throw (
                "Local .venv is non-empty but incomplete. Move it aside " +
                "before retrying: $venvDirectory"
            )
        }
        & $script:BasePythonPath -m venv $venvDirectory
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "Creating the local virtual environment failed."
        }
    }
    Assert-PythonExecutable -PythonPath $venvPython

    $orderedMirrors = Get-ReachableMirrors `
        -Mirrors $pipMirrors `
        -ProbeProperty "Probe"
    if ($orderedMirrors.Count -eq 0) {
        throw "No domestic PyPI mirror is reachable."
    }
    $installed = $false
    foreach ($mirror in $orderedMirrors) {
        Write-Output "使用$($mirror.Name)安装Python依赖。"
        & $venvPython `
            -m pip install `
            --disable-pip-version-check `
            --no-input `
            --index-url $mirror.Index `
            --upgrade pip
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            continue
        }
        & $venvPython `
            -m pip install `
            --disable-pip-version-check `
            --no-input `
            --index-url $mirror.Index `
            --requirement $requirementsPath
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            continue
        }
        & $venvPython `
            -m pip config `
            --site set `
            global.index-url `
            $mirror.Index
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "Saving the site-local pip mirror failed."
        }
        $installed = $true
        break
    }
    if (-not $installed) {
        throw "Installing dependencies from domestic PyPI mirrors failed."
    }
    Assert-PythonExecutable -PythonPath $venvPython -RequireXtQuant
}

function Resolve-QmtUserdataPath {
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)]
        [ValidateSet("live", "simulation")]
        [string]$Environment,
        [Parameter(Mandatory = $true)][string]$DefaultInstallRoot,
        [string]$Existing = ""
    )
    $suggestedRoot = $DefaultInstallRoot
    if (-not [string]::IsNullOrWhiteSpace($Existing)) {
        $existingPath = [System.IO.Path]::GetFullPath(
            [Environment]::ExpandEnvironmentVariables(
                $Existing.Trim().Trim('"')
            )
        )
        $suggestedRoot = if (
            (Split-Path -Leaf $existingPath) -eq "userdata_mini"
        ) {
            Split-Path -Parent $existingPath
        }
        else {
            $existingPath
        }
    }
    $inputPath = Read-Host "$Prompt [$suggestedRoot]"
    if ([string]::IsNullOrWhiteSpace($inputPath)) {
        $inputPath = $suggestedRoot
    }
    $inputPath = [Environment]::ExpandEnvironmentVariables(
        $inputPath.Trim().Trim('"')
    )
    if ([string]::IsNullOrWhiteSpace($inputPath)) {
        throw "miniQMT安装目录不能为空。"
    }
    $resolved = [System.IO.Path]::GetFullPath($inputPath)
    $installRoot = if ((Split-Path -Leaf $resolved) -eq "userdata_mini") {
        Split-Path -Parent $resolved
    }
    else {
        $resolved
    }
    $label = if ($Environment -eq "live") { "实盘" } else { "模拟" }
    if ($Environment -eq "live" -and $installRoot -match "模拟") {
        throw (
            "实盘miniQMT安装目录不能包含【模拟】，两个路径可能填反了：" +
            $installRoot
        )
    }
    if ($Environment -eq "simulation" -and $installRoot -notmatch "模拟") {
        throw (
            "模拟miniQMT安装目录应包含【模拟】，两个路径可能填反了：" +
            $installRoot
        )
    }
    if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
        throw "未找到${label}miniQMT安装目录，请先完成安装：$installRoot"
    }

    $userdataPath = Join-Path $installRoot "userdata_mini"
    while (-not (Test-Path -LiteralPath $userdataPath -PathType Container)) {
        Write-Warning (
            "尚未检测到 $userdataPath。该目录会在首次使用【独立交易】" +
            "登录后由miniQMT创建。"
        )
        $answer = Read-Host (
            "请启动${label}miniQMT，勾选【独立交易】并登录一次；" +
            "完成后输入Y重试，输入N退出 [Y/n]"
        )
        if ($answer.Trim() -match "^[Nn]$") {
            throw (
                "${label}miniQMT尚未完成独立交易登录；" +
                "完成后重新运行 .\rr init。"
            )
        }
    }
    return $userdataPath
}

function Initialize-RuntimeConfiguration {
    $existing = $null
    if (Test-Path -LiteralPath $runtimeConfigPath -PathType Leaf) {
        $existing = Get-Content -LiteralPath $runtimeConfigPath -Raw |
            ConvertFrom-Json
    }
    $existingLive = if ($null -eq $existing) {
        ""
    }
    else {
        [string]$existing.live_qmt_path
    }
    $existingSimulation = if ($null -eq $existing) {
        ""
    }
    else {
        [string]$existing.simulation_qmt_path
    }
    $livePath = Resolve-QmtUserdataPath `
        -Prompt "实盘miniQMT安装目录" `
        -Environment "live" `
        -DefaultInstallRoot "D:\国金证券QMT交易端" `
        -Existing $existingLive
    $simulationPath = Resolve-QmtUserdataPath `
        -Prompt "模拟miniQMT安装目录" `
        -Environment "simulation" `
        -DefaultInstallRoot "D:\国金QMT交易端模拟" `
        -Existing $existingSimulation
    $firstTime = if ($null -eq $existing) {
        "09:30:42"
    }
    else {
        [string]$existing.first_execution_time
    }
    $secondTime = if ($null -eq $existing) {
        "15:10:00"
    }
    else {
        [string]$existing.second_execution_time
    }
    $firstRatio = if ($null -eq $existing) {
        0.90
    }
    else {
        $existing.first_cash_usage_ratio
    }
    $secondRatio = if ($null -eq $existing) {
        1.00
    }
    else {
        $existing.second_cash_usage_ratio
    }
    $config = [ordered]@{
        python_path = ".venv\Scripts\python.exe"
        live_qmt_path = $livePath
        simulation_qmt_path = $simulationPath
        first_execution_time = $firstTime
        second_execution_time = $secondTime
        first_cash_usage_ratio = $firstRatio
        second_cash_usage_ratio = $secondRatio
    }
    Write-Utf8NoBom `
        -Path $runtimeConfigPath `
        -Text ($config | ConvertTo-Json -Depth 4)
    $script:ReverseRepoRuntimeConfig = $null
    $null = Get-ReverseRepoFirstExecutionTime
    $null = Get-ReverseRepoSecondExecutionTime
    $null = Get-ReverseRepoFirstCashUsageRatio
    $null = Get-ReverseRepoSecondCashUsageRatio
}

function Initialize-SigningKey {
    & $venvPython `
        (Join-Path $PSScriptRoot "bootstrap_repo_release_gate_secret.py") `
        "--output" `
        $signingKeyPath
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Creating the local release-gate signing key failed."
    }
}

function Initialize-AccountBinding {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("live", "simulation")]
        [string]$Environment
    )
    $bindingName = if ($Environment -eq "live") {
        "repo_live_account_binding.local.json"
    }
    else {
        "repo_simulation_account_binding.local.json"
    }
    $bindingPath = Join-Path $repoRoot "config\$bindingName"
    if (Test-Path -LiteralPath $bindingPath -PathType Leaf) {
        Write-Output "$Environment account binding already exists."
        return $true
    }
    $label = if ($Environment -eq "live") { "实盘" } else { "模拟" }
    Write-Output "请启动并登录${label}miniQMT；绑定操作只查询账户，不下单。"
    $answer = Read-Host "准备好后输入Y继续，输入N稍后手工绑定 [Y/n]"
    if (
        -not [string]::IsNullOrWhiteSpace($answer) `
        -and $answer.Trim().ToLowerInvariant() -eq "n"
    ) {
        Write-Warning "已跳过${label}账户绑定。"
        return $false
    }
    & $windowsPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $repoRoot "bind.ps1") `
        $Environment
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "$Environment account binding failed."
    }
    return $true
}

Assert-LiveTasksInactive
Install-PrivatePython
Install-VirtualEnvironment
Initialize-RuntimeConfiguration
Initialize-SigningKey

& $windowsPowerShell `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File (Join-Path $repoRoot "verify.ps1")
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Local verification failed during initialization."
}

$bindingsReady = $true
if (-not $SkipAccountBinding) {
    $bindingsReady = (
        (Initialize-AccountBinding -Environment "live") `
        -and (Initialize-AccountBinding -Environment "simulation")
    )
}
if (-not $SkipTaskInstall -and $bindingsReady) {
    & $windowsPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot "manage_reverse_repo_tasks.ps1") `
        -Action Install
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Installing disabled live tasks failed."
    }
}

Write-Output "reverse_repo初始化完成；实盘任务未启用。"
if (-not $bindingsReady) {
    Write-Output "账户绑定尚未完成，之后执行 .\bind.ps1 live/simulation。"
}
Write-Output "下一步：.\rr stat，然后用一个完整交易日执行 .\rr cert。"
