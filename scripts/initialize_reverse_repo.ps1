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
$pythonPackageSources = @(
    [pscustomobject]@{
        Name = "华为云"
        Uri = (
            "https://mirrors.huaweicloud.com/python/3.12.10/" +
            "python-3.12.10-amd64.zip"
        )
    },
    [pscustomobject]@{
        Name = "npmmirror"
        Uri = (
            "https://registry.npmmirror.com/-/binary/python/3.12.10/" +
            "python-3.12.10-amd64.zip"
        )
    }
)
$pythonPackageSize = 32399384
$pythonPackageSha256 = (
    "9dc4d0b051bfd5b881f10846ee023fd7cea8251871e78b6e8920e5630b15e3bb"
)
$runtimeDirectory = Join-Path $repoRoot ".runtime\python312"
$runtimePython = Join-Path $runtimeDirectory "python.exe"
$venvDirectory = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$dependencyStatePath = Join-Path `
    $venvDirectory `
    "reverse_repo_dependencies.json"
$runtimeConfigPath = Join-Path $repoRoot "config\runtime.local.json"
$requirementsPath = Join-Path $repoRoot "requirements.txt"
$bootstrapDirectory = Join-Path $repoRoot "tmp\bootstrap"
$signingKeyPath = Join-Path `
    $repoRoot `
    "config\repo_release_gate_secret.local.json"

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

function Get-VerifiedRemoteFile {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][long]$ExpectedSize,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    $lastError = $null
    foreach ($attempt in 1..3) {
        try {
            Invoke-WebRequest `
                -UseBasicParsing `
                -Uri $Uri `
                -OutFile $Path `
                -Headers @{ "Cache-Control" = "no-cache" } `
                -TimeoutSec 300
            $actualSize = (Get-Item -LiteralPath $Path).Length
            $actualHash = Get-ReverseRepoSha256 -Path $Path
            if (
                [long]$actualSize -ne $ExpectedSize -or
                $actualHash -ne $ExpectedSha256.ToLowerInvariant()
            ) {
                throw "Downloaded file failed size or SHA-256 check."
            }
            return
        }
        catch {
            $lastError = $_
            if (Test-Path -LiteralPath $Path -PathType Leaf) {
                Remove-Item -LiteralPath $Path -Force
            }
            if ($attempt -lt 3) {
                Start-Sleep -Seconds 2
            }
        }
    }
    throw "Download failed after three attempts: $Uri - $lastError"
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

function Install-PortablePython {
    if (Test-Path -LiteralPath $runtimePython -PathType Leaf) {
        Assert-PythonExecutable -PythonPath $runtimePython
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
    $stagingRoot = Join-Path `
        $bootstrapDirectory `
        ("python_" + [guid]::NewGuid().ToString("N"))
    try {
        New-Item -ItemType Directory -Force -Path $stagingRoot | Out-Null
        $pythonPackagePath = Join-Path `
            $stagingRoot `
            "python-3.12.10-amd64.zip"
        $downloaded = $false
        $downloadErrors = @()
        foreach ($source in $pythonPackageSources) {
            $sourceName = [string]$source.Name
            Write-Output (
                "从${sourceName}下载便携Python $pythonVersion x64。"
            )
            try {
                Get-VerifiedRemoteFile `
                    -Uri ([string]$source.Uri) `
                    -Path $pythonPackagePath `
                    -ExpectedSize $pythonPackageSize `
                    -ExpectedSha256 $pythonPackageSha256
                $downloaded = $true
                break
            }
            catch {
                $downloadErrors += "${sourceName}: $($_.Exception.Message)"
                Write-Warning "${sourceName}下载失败，尝试下一个备用源。"
            }
        }
        if (-not $downloaded) {
            throw (
                "所有便携Python下载源均失败：" +
                ($downloadErrors -join " | ")
            )
        }

        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead(
            $pythonPackagePath
        )
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
                    throw (
                        "Portable Python package is incomplete: " +
                        "$requiredEntry is missing."
                    )
                }
            }
        }
        finally {
            $archive.Dispose()
        }
        $stagingRuntime = Join-Path $stagingRoot "runtime"
        New-Item -ItemType Directory -Path $stagingRuntime | Out-Null
        [System.IO.Compression.ZipFile]::ExtractToDirectory(
            $pythonPackagePath,
            $stagingRuntime
        )
        # The official install-manager ZIP carries the two standard venv
        # launchers under Lib, while venv expects root copies in this
        # installer-free layout. Copying them stays inside the private runtime.
        foreach ($launcher in @("venvlauncher.exe", "venvwlauncher.exe")) {
            Copy-Item `
                -LiteralPath (Join-Path `
                    $stagingRuntime `
                    ("Lib\venv\scripts\nt\" + $launcher)) `
                -Destination (Join-Path $stagingRuntime $launcher)
        }
        $stagingPython = Join-Path $stagingRuntime "python.exe"
        Assert-PythonExecutable -PythonPath $stagingPython
        New-Item `
            -ItemType Directory `
            -Force `
            -Path (Split-Path -Parent $runtimeDirectory) |
            Out-Null
        if (Test-Path -LiteralPath $runtimeDirectory) {
            Remove-Item -LiteralPath $runtimeDirectory -Force
        }
        Move-Item `
            -LiteralPath $stagingRuntime `
            -Destination $runtimeDirectory
    }
    finally {
        if (Test-Path -LiteralPath $stagingRoot) {
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
    }
    Write-Output "已在项目目录内展开便携Python $pythonVersion x64。"
    Assert-PythonExecutable -PythonPath $runtimePython
}

function Get-RequirementsSha256 {
    return (Get-ReverseRepoSha256 -Path $requirementsPath)
}

function Test-VirtualEnvironmentReady {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        return $false
    }
    $requirementsHash = Get-RequirementsSha256
    if (Test-Path -LiteralPath $dependencyStatePath -PathType Leaf) {
        try {
            $state = Read-ReverseRepoJson -Path $dependencyStatePath
            if (
                [int]$state.schema_version -ne 1 `
                -or [string]$state.python_version -ne $pythonVersion `
                -or [string]$state.requirements_sha256 -ne $requirementsHash
            ) {
                return $false
            }
        }
        catch {
            return $false
        }
    }

    # The marker may be absent on environments created by an older release.
    # Prove the current pinned requirement and imports locally before adopting
    # that environment; no package index or network connection is consulted.
    $requirements = @(
        [System.IO.File]::ReadAllLines(
            $requirementsPath,
            [System.Text.Encoding]::UTF8
        ) |
            ForEach-Object { ($_ -split "#", 2)[0].Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    $pinnedRequirements = @()
    foreach ($requirement in $requirements) {
        if (
            $requirement -notmatch `
                "^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)$"
        ) {
            return $false
        }
        $pinnedRequirements += [pscustomobject]@{
            Name = [string]$Matches[1]
            Version = [string]$Matches[2]
        }
    }
    if (
        @($pinnedRequirements | Where-Object { $_.Name -ieq "xtquant" }).Count `
            -ne 1
    ) {
        return $false
    }
    $probe = (
        "import struct,sys; " +
        "assert sys.version_info[:3]==(3,12,10); " +
        "assert struct.calcsize('P')*8==64; " +
        "from xtquant import xtconstant,xtdata,xttype; " +
        "from xtquant.xttrader import XtQuantTrader"
    )
    $versionProbe = (
        "import importlib.metadata as m,sys; " +
        "assert m.version(sys.argv[1])==sys.argv[2]"
    )
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $venvPython -c $probe 1>$null 2>$null
        $probeExitCode = $LASTEXITCODE
        $pinnedVersionsValid = $true
        foreach ($requirement in $pinnedRequirements) {
            & $venvPython -c `
                $versionProbe `
                $requirement.Name `
                $requirement.Version `
                1>$null 2>$null
            if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
                $pinnedVersionsValid = $false
                break
            }
        }
        & $venvPython -m pip check 1>$null 2>$null
        $pipCheckExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    if (
        $null -eq $probeExitCode `
        -or [int]$probeExitCode -ne 0 `
        -or -not $pinnedVersionsValid `
        -or $null -eq $pipCheckExitCode `
        -or [int]$pipCheckExitCode -ne 0
    ) {
        return $false
    }
    return $true
}

function Save-VirtualEnvironmentState {
    $state = [ordered]@{
        schema_version = 1
        python_version = $pythonVersion
        requirements_sha256 = (Get-RequirementsSha256)
        verified_at = (Get-Date).ToString("o")
    }
    Write-Utf8NoBom `
        -Path $dependencyStatePath `
        -Text ($state | ConvertTo-Json -Depth 3)
}

function Install-VirtualEnvironment {
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
        & $runtimePython -m venv $venvDirectory
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "Creating the local virtual environment failed."
        }
    }
    Assert-PythonExecutable -PythonPath $venvPython

    if (Test-VirtualEnvironmentReady) {
        if (-not (Test-Path -LiteralPath $dependencyStatePath -PathType Leaf)) {
            Save-VirtualEnvironmentState
        }
        Write-Output "本地Python依赖完整且版本匹配，跳过联网安装。"
        return
    }

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
    & $venvPython -m pip check
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Installed Python dependencies are inconsistent."
    }
    Save-VirtualEnvironmentState
}

function Get-RunningMiniQmtInstallRoot {
    try {
        $processes = @(
            Get-CimInstance `
                -ClassName Win32_Process `
                -Filter "Name='XtMiniQmt.exe'" `
                -ErrorAction Stop
        )
    }
    catch {
        Write-Warning (
            "无法读取运行中的miniQMT进程路径，将使用配置或默认目录：" +
            $_.Exception.Message
        )
        return ""
    }

    $candidates = @()
    foreach ($process in $processes) {
        $executablePath = [string]$process.ExecutablePath
        if ([string]::IsNullOrWhiteSpace($executablePath)) {
            continue
        }
        try {
            $binDirectory = Split-Path -Parent (
                [System.IO.Path]::GetFullPath($executablePath)
            )
            if ((Split-Path -Leaf $binDirectory) -ne "bin.x64") {
                continue
            }
            $installRoot = Split-Path -Parent $binDirectory
            if (-not (
                Test-Path -LiteralPath $installRoot -PathType Container
            )) {
                continue
            }
            if ($installRoot -notmatch "模拟|仿真|simulation") {
                $candidates += $installRoot
            }
        }
        catch {
            continue
        }
    }
    $candidates = @($candidates | Sort-Object -Unique)
    if ($candidates.Count -eq 1) {
        Write-Host "已从运行中的实盘miniQMT发现安装目录：$($candidates[0])"
        return [string]$candidates[0]
    }
    if ($candidates.Count -gt 1) {
        Write-Warning (
            "发现多个运行中的实盘miniQMT安装目录，" +
            "为避免误选，将使用配置或默认目录：" +
            ($candidates -join "; ")
        )
    }
    return ""
}

function Resolve-QmtUserdataPath {
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$DefaultInstallRoot,
        [string]$DetectedInstallRoot = "",
        [string]$Existing = ""
    )
    $suggestedRoot = if (
        [string]::IsNullOrWhiteSpace($DetectedInstallRoot)
    ) {
        $DefaultInstallRoot
    }
    else {
        [System.IO.Path]::GetFullPath($DetectedInstallRoot)
    }
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
    if ($installRoot -match "模拟") {
        throw (
            "实盘miniQMT安装目录不能包含【模拟】，两个路径可能填反了：" +
            $installRoot
        )
    }
    if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
        throw "未找到实盘miniQMT安装目录，请先完成安装：$installRoot"
    }

    $userdataPath = Join-Path $installRoot "userdata_mini"
    while (-not (Test-Path -LiteralPath $userdataPath -PathType Container)) {
        Write-Warning (
            "尚未检测到 $userdataPath。该目录会在首次使用【独立交易】" +
            "登录后由miniQMT创建。"
        )
        $answer = Read-Host (
            "请启动实盘miniQMT，勾选【独立交易】并登录一次；" +
            "完成后输入Y重试，输入N退出 [Y/n]"
        )
        if ($answer.Trim() -match "^[Nn]$") {
            throw (
                "实盘miniQMT尚未完成独立交易登录；" +
                "完成后重新运行 .\rr init。"
            )
        }
    }
    return $userdataPath
}

function Initialize-RuntimeConfiguration {
    $existing = $null
    if (Test-Path -LiteralPath $runtimeConfigPath -PathType Leaf) {
        $existing = Read-ReverseRepoJson -Path $runtimeConfigPath
    }
    $existingLive = if ($null -eq $existing) {
        ""
    }
    else {
        [string]$existing.live_qmt_path
    }
    $detectedLiveRoot = Get-RunningMiniQmtInstallRoot
    $livePath = Resolve-QmtUserdataPath `
        -Prompt "实盘miniQMT安装目录" `
        -DefaultInstallRoot "D:\国金证券QMT交易端" `
        -DetectedInstallRoot $detectedLiveRoot `
        -Existing $existingLive
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
    $bindingName = "repo_live_account_binding.local.json"
    $bindingPath = Join-Path $repoRoot "config\$bindingName"
    if (Test-Path -LiteralPath $bindingPath -PathType Leaf) {
        Write-Host "Live account binding already exists."
        return $true
    }
    Write-Host "请启动并登录实盘miniQMT；绑定操作只查询账户，不下单。"
    $answer = Read-Host "准备好后输入Y继续，输入N稍后手工绑定 [Y/n]"
    if (
        -not [string]::IsNullOrWhiteSpace($answer) `
        -and $answer.Trim().ToLowerInvariant() -eq "n"
    ) {
        Write-Warning "已跳过实盘账户绑定。"
        return $false
    }
    & $windowsPowerShell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $repoRoot "bind.ps1") |
        Out-Host
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Live account binding failed."
    }
    return $true
}

Assert-LiveTasksInactive
Install-PortablePython
Install-VirtualEnvironment
Initialize-RuntimeConfiguration
Initialize-SigningKey

& $windowsPowerShell `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File (Join-Path $repoRoot "verify.ps1") `
    -Initialization
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Local verification failed during initialization."
}

$bindingsReady = $true
if (-not $SkipAccountBinding) {
    $bindingsReady = Initialize-AccountBinding
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
    Write-Output "实盘账户绑定尚未完成，之后执行 .\bind.ps1。"
}
Write-Output "下一步：.\rr ui 打开网页控制台，完成参数确认、实盘认证和启停；命令行入口仍可用。"
