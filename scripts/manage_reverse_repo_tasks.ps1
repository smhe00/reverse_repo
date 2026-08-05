[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "Install",
        "Remove",
        "Clear",
        "Status",
        "Enable",
        "Disable",
        "ConfigureMail",
        "TestMail",
        "LiveCert",
        "LiveCertPreflight",
        "LiveCertStatus",
        "LiveCertReset",
        "DevBind",
        "DevStatus",
        "DevCert",
        "DevCertDisable",
        "DevCertRemove",
        "DevCertStatus",
        "DevCertReset",
        "DevStress",
        "DevStressDisable",
        "DevStressRemove",
        "DevStressStatus",
        "Initialize",
        "Help"
    )]
    [string]$Action,
    [string]$LiveCertConfirmation = "",
    [string]$CertDate = "",
    [string]$StressDate = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$managedTaskNames = @(
    "miniQMT Reverse Repo First",
    "miniQMT Reverse Repo Second"
)
$stressTaskName = "miniQMT SIM Interface Stress 5Hz"
$certTaskNames = @(
    "miniQMT SIM Repo V3 Morning Normal",
    "miniQMT SIM Repo V3 Afternoon Normal",
    "miniQMT SIM Repo V3 Morning Recovery",
    "miniQMT SIM Repo V3 Certificate"
)
$obsoleteCertTaskNames = @(
    "miniQMT SIM Repo V2 First Recovery",
    "miniQMT SIM Repo V2 Second",
    "miniQMT SIM Repo V2 Certificate",
    "miniQMT SIM Repo V2 Morning Recovery",
    "miniQMT SIM Repo V2 Afternoon"
)
$readOnlyTaskNames = @(
    "miniQMT LIVE READONLY Morning",
    "miniQMT LIVE READONLY Afternoon"
)
$legacyProjectTaskNames = @(
    "miniQMT Backtest DB Update Yesterday"
)
$obsoleteTaskNames = @(
    "miniQMT Reverse Repo Once",
    "miniQMT GC001 Daily 90pct 093042",
    "miniQMT GC001 R001 Afternoon Sweep",
    "miniQMT Reverse Repo Morning",
    "miniQMT Reverse Repo Afternoon"
)
$allReverseRepoTaskNames = @(
    $managedTaskNames +
        $obsoleteTaskNames +
        $certTaskNames +
        $obsoleteCertTaskNames +
        $readOnlyTaskNames +
        @($stressTaskName) +
        $legacyProjectTaskNames |
        Sort-Object -Unique
)
$taskDefinitions = @()
$firstExecutionText = "未加载"
$secondExecutionText = "未加载"
$firstStartText = "未加载"
$secondStartText = "未加载"
$firstCashUsagePercent = "未加载"
$secondCashUsagePercent = "未加载"

function Initialize-TaskDefinitions {
    if ($taskDefinitions.Count -gt 0) {
        return
    }
    $firstExecution = Get-ReverseRepoFirstExecutionTime
    $secondExecution = Get-ReverseRepoSecondExecutionTime
    $script:firstExecutionText = Format-ReverseRepoClockTime $firstExecution
    $script:secondExecutionText = Format-ReverseRepoClockTime $secondExecution
    $script:firstStartText = Format-ReverseRepoClockTime `
        (Get-ReverseRepoTaskStartTime `
            -ExecutionTime $firstExecution `
            -LeadSeconds 162)
    $script:secondStartText = Format-ReverseRepoClockTime `
        (Get-ReverseRepoTaskStartTime `
            -ExecutionTime $secondExecution `
            -LeadSeconds 120)
    $firstCashUsageRatio = Get-ReverseRepoFirstCashUsageRatio
    $secondCashUsageRatio = Get-ReverseRepoSecondCashUsageRatio
    $script:firstCashUsagePercent = "{0:P0}" -f $firstCashUsageRatio
    $script:secondCashUsagePercent = "{0:P0}" -f $secondCashUsageRatio
    $script:taskDefinitions = @(
        [pscustomobject]@{
            Name = $managedTaskNames[0]
            Wrapper = Join-Path `
                $PSScriptRoot `
                "run_gc001_daily_90pct_093042.ps1"
            StartAt = $firstStartText
            ExecutionMinutes = 10
            EnabledByConfig = ($firstCashUsageRatio -gt 0)
            Parameters = (
                "first_order=$firstExecutionText; " +
                "cash_usage=$firstCashUsagePercent"
            )
            Description = (
                "GC001 first state machine v2: $firstExecutionText, " +
                "$firstCashUsagePercent verified live cash."
            )
        },
        [pscustomobject]@{
            Name = $managedTaskNames[1]
            Wrapper = Join-Path `
                $PSScriptRoot `
                "run_gc001_r001_afternoon_sweep.ps1"
            StartAt = $secondStartText
            ExecutionMinutes = 390
            EnabledByConfig = ($secondCashUsageRatio -gt 0)
            Parameters = (
                "second_start=$secondExecutionText; " +
                "cash_usage=$secondCashUsagePercent"
            )
            Description = (
                "GC001/R-001 second state machine v2: " +
                "$secondExecutionText, $secondCashUsagePercent of " +
                "verified available cash."
            )
        }
    )
}

function Get-ManagedTaskStatus {
    Initialize-TaskDefinitions
    $expectedPowerShell = Get-ReverseRepoPowerShell
    foreach ($definition in $taskDefinitions) {
        $task = Get-ScheduledTask `
            -TaskName $definition.Name `
            -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            [pscustomobject]@{
                TaskName = $definition.Name
                Installed = $false
                State = "NotInstalled"
                StrategyParameters = $definition.Parameters
                EnabledByConfig = $definition.EnabledByConfig
                Schedule = "未安装；配置为周一至周五 $($definition.StartAt)"
                ScheduleMatchesConfig = $false
                LiveEnableSnapshot = if (
                    Test-Path -LiteralPath (
                        Get-ReverseRepoLiveEnableManifestPath
                    ) -PathType Leaf
                ) { "存在" } else { "不存在" }
                NextRunTime = $null
                LastRunTime = $null
                LastResult = $null
            }
            continue
        }
        $info = Get-ScheduledTaskInfo -TaskName $definition.Name
        $nextRunTime = $info.NextRunTime
        if ($nextRunTime -gt [datetime]::MinValue) {
            $nextRunTime = $nextRunTime.AddSeconds(
                -$nextRunTime.Second
            )
        }
        $isDisabled = ([string]$task.State -eq "Disabled")
        $trigger = $task.Triggers | Select-Object -First 1
        $action = $task.Actions | Select-Object -First 1
        $actualStart = ([datetime]$trigger.StartBoundary).ToString(
            "HH:mm:ss"
        )
        $expectedArguments = (
            '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f `
                $definition.Wrapper
        )
        $hasNeverRun = (
            [int64]$info.LastTaskResult -eq 267011 `
            -or $info.LastRunTime.Year -lt 2000
        )
        [pscustomobject]@{
            TaskName = $definition.Name
            Installed = $true
            State = [string]$task.State
            StrategyParameters = $definition.Parameters
            EnabledByConfig = $definition.EnabledByConfig
            Schedule = "周一至周五 $actualStart"
            ScheduleMatchesConfig = (
                $actualStart -eq $definition.StartAt `
                -and [string]$action.Execute -ieq $expectedPowerShell `
                -and [string]$action.WorkingDirectory -eq $repoRoot `
                -and [string]$action.Arguments -eq $expectedArguments
            )
            LiveEnableSnapshot = if (
                Test-Path -LiteralPath (
                    Get-ReverseRepoLiveEnableManifestPath
                ) -PathType Leaf
            ) { "存在" } else { "不存在" }
            NextRunTime = if ($isDisabled) {
                "已禁用，不会运行"
            }
            else {
                $nextRunTime
            }
            LastRunTime = if ($hasNeverRun) {
                "尚未运行"
            }
            else {
                $info.LastRunTime
            }
            LastResult = Format-TaskResult `
                -Result ([int64]$info.LastTaskResult)
        }
    }
}

function Format-TaskResult {
    param([Parameter(Mandatory = $true)][int64]$Result)
    switch ($Result) {
        0 { return "成功 (0)" }
        267008 { return "任务已就绪 (0x41300)" }
        267009 { return "正在运行 (0x41301)" }
        267010 { return "任务已禁用 (0x41302)" }
        267011 { return "尚未运行 (0x41303)" }
        267012 { return "没有后续计划 (0x41304)" }
        267013 { return "任务未计划 (0x41305)" }
        267014 { return "任务已终止 (0x41306)" }
        default {
            return (
                "代码 {0} (0x{1:X8})" -f $Result, $Result
            )
        }
    }
}

function Install-ManagedTasks {
    Initialize-TaskDefinitions
    $runningTasks = @(
        foreach ($definition in $taskDefinitions) {
            $existing = Get-ScheduledTask `
                -TaskName $definition.Name `
                -ErrorAction SilentlyContinue
            if ($null -ne $existing -and $existing.State -eq "Running") {
                $definition.Name
            }
        }
    )
    if ($runningTasks.Count -ne 0) {
        throw (
            "Cannot install or update running live tasks: " +
            ($runningTasks -join ", ")
        )
    }
    foreach ($definition in $taskDefinitions) {
        if (-not (Test-Path -LiteralPath $definition.Wrapper)) {
            throw "Task wrapper does not exist: $($definition.Wrapper)"
        }
    }
    if ($PSCmdlet.ShouldProcess(
        (Get-ReverseRepoLiveEnableManifestPath),
        "Revoke live-enable snapshot before task installation"
    )) {
        Remove-ReverseRepoLiveEnableManifest
    }
    $powerShellPath = Get-ReverseRepoPowerShell
    $userId = (
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    )
    foreach ($definition in $taskDefinitions) {
        if (-not $PSCmdlet.ShouldProcess(
            $definition.Name,
            "Install or update Windows scheduled task"
        )) {
            continue
        }
        $taskAction = New-ScheduledTaskAction `
            -Execute $powerShellPath `
            -Argument (
                '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f `
                    $definition.Wrapper
            ) `
            -WorkingDirectory $repoRoot
        $trigger = New-ScheduledTaskTrigger `
            -Weekly `
            -WeeksInterval 1 `
            -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
            -At $definition.StartAt
        $settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (
                New-TimeSpan -Minutes $definition.ExecutionMinutes
            ) `
            -MultipleInstances IgnoreNew `
            -WakeToRun `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal `
            -UserId $userId `
            -LogonType Interactive `
            -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $definition.Name `
            -Action $taskAction `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Description $definition.Description `
            -Force |
            Out-Null
        # Installation and updates never arm live trading implicitly.
        Disable-ScheduledTask -TaskName $definition.Name | Out-Null
    }
    foreach ($obsoleteTaskName in $obsoleteTaskNames) {
        $obsolete = Get-ScheduledTask `
            -TaskName $obsoleteTaskName `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $obsolete `
            -and $PSCmdlet.ShouldProcess(
                $obsoleteTaskName,
                "Remove obsolete reverse-repo task"
            )
        ) {
            Disable-ScheduledTask -TaskName $obsoleteTaskName | Out-Null
            Unregister-ScheduledTask `
                -TaskName $obsoleteTaskName `
                -Confirm:$false
        }
    }
}

function Remove-ManagedTasks {
    if ($PSCmdlet.ShouldProcess(
        (Get-ReverseRepoLiveEnableManifestPath),
        "Revoke live-enable snapshot before task removal"
    )) {
        Remove-ReverseRepoLiveEnableManifest
    }
    $taskNames = $managedTaskNames + $obsoleteTaskNames
    foreach ($taskName in $taskNames) {
        $task = Get-ScheduledTask `
            -TaskName $taskName `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $task `
            -and $PSCmdlet.ShouldProcess(
                $taskName,
                "Remove Windows scheduled task"
            )
        ) {
            Unregister-ScheduledTask `
                -TaskName $taskName `
                -Confirm:$false
        }
    }
}

function Clear-AllReverseRepoTasks {
    if ($PSCmdlet.ShouldProcess(
        (Get-ReverseRepoLiveEnableManifestPath),
        "Revoke live-enable snapshot before clearing all project tasks"
    )) {
        Remove-ReverseRepoLiveEnableManifest
    }

    $installed = @(
        foreach ($taskName in $allReverseRepoTaskNames) {
            $task = Get-ScheduledTask `
                -TaskName $taskName `
                -ErrorAction SilentlyContinue
            if ($null -ne $task) {
                $task
            }
        }
    )
    foreach ($task in $installed) {
        if ($PSCmdlet.ShouldProcess(
            $task.TaskName,
            "Disable project task before removal"
        )) {
            Disable-ScheduledTask -TaskName $task.TaskName | Out-Null
        }
    }

    if ($WhatIfPreference) {
        foreach ($task in $installed) {
            $null = $PSCmdlet.ShouldProcess(
                $task.TaskName,
                "Remove project task"
            )
        }
        Write-Output (
            "WhatIf：将清除$($installed.Count)项reverse_repo计划任务。"
        )
        return
    }

    $running = @(
        foreach ($taskName in $allReverseRepoTaskNames) {
            $task = Get-ScheduledTask `
                -TaskName $taskName `
                -ErrorAction SilentlyContinue
            if ($null -ne $task -and [string]$task.State -eq "Running") {
                $task.TaskName
            }
        }
    )
    if ($running.Count -gt 0) {
        throw (
            "已禁用所有后续触发，但以下任务仍在运行，未强制终止：" +
            ($running -join ", ") +
            "。等待其结束后再次执行 .\rr clear。"
        )
    }

    $removedNames = @()
    foreach ($taskName in $allReverseRepoTaskNames) {
        $task = Get-ScheduledTask `
            -TaskName $taskName `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $task -and
            $PSCmdlet.ShouldProcess($taskName, "Remove project task")
        ) {
            Unregister-ScheduledTask `
                -TaskName $taskName `
                -Confirm:$false
            $removedNames += $taskName
        }
    }
    $remaining = @(
        foreach ($taskName in $allReverseRepoTaskNames) {
            if ($null -ne (Get-ScheduledTask `
                -TaskName $taskName `
                -ErrorAction SilentlyContinue)) {
                $taskName
            }
        }
    )
    if ($remaining.Count -ne 0) {
        throw (
            "计划任务清理不完整，仍有残留：" +
            ($remaining -join ", ")
        )
    }
    Write-Output (
        "已清除全部reverse_repo计划任务，共$($removedNames.Count)项；" +
        "残留0项。代码、配置、绑定、证书和报告均保留。"
    )
}

function Set-ManagedTasksEnabled {
    param([Parameter(Mandatory = $true)][bool]$Enabled)
    $manifestCreated = $false
    if ($Enabled) {
        # Reconcile task definitions while they are disabled before arming.
        Install-ManagedTasks
        Initialize-TaskDefinitions
        Assert-ManagedTasksMatchConfig
        $activeDefinitions = @(
            $taskDefinitions | Where-Object { $_.EnabledByConfig }
        )
        if ($activeDefinitions.Count -gt 0) {
            Assert-LiveEnableGate
            if ($PSCmdlet.ShouldProcess(
                (Get-ReverseRepoLiveEnableManifestPath),
                "Create signed live-enable snapshot"
            )) {
                New-ReverseRepoLiveEnableManifest
                $manifestCreated = $true
            }
            else {
                Write-Output (
                    "Live-enable snapshot was not created; no live task " +
                    "will be enabled."
                )
                return
            }
        }
        else {
            Remove-ReverseRepoLiveEnableManifest
            Write-Output (
                "Both cash usage ratios are 0; no live task will be enabled."
            )
        }
    }
    elseif ($PSCmdlet.ShouldProcess(
        (Get-ReverseRepoLiveEnableManifestPath),
        "Revoke live-enable snapshot before disabling tasks"
    )) {
        Remove-ReverseRepoLiveEnableManifest
    }
    $names = $managedTaskNames + $obsoleteTaskNames
    try {
        foreach ($name in $names) {
            $task = Get-ScheduledTask `
                -TaskName $name `
                -ErrorAction SilentlyContinue
            if ($null -eq $task) {
                continue
            }
            $definition = $taskDefinitions |
                Where-Object { $_.Name -eq $name } |
                Select-Object -First 1
            $enableThisTask = (
                $Enabled `
                -and $null -ne $definition `
                -and $definition.EnabledByConfig
            )
            $verb = if ($enableThisTask) { "Enable" } else { "Disable" }
            if (-not $PSCmdlet.ShouldProcess($name, "$verb task")) {
                continue
            }
            if ($enableThisTask) {
                Enable-ScheduledTask -TaskName $name | Out-Null
            }
            else {
                Disable-ScheduledTask -TaskName $name | Out-Null
            }
        }
    }
    catch {
        if ($manifestCreated) {
            Remove-ReverseRepoLiveEnableManifest
        }
        foreach ($name in $managedTaskNames) {
            Disable-ScheduledTask `
                -TaskName $name `
                -ErrorAction SilentlyContinue |
                Out-Null
        }
        throw
    }
}

function Assert-ManagedTasksMatchConfig {
    foreach ($definition in $taskDefinitions) {
        $task = Get-ScheduledTask `
            -TaskName $definition.Name `
            -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            throw (
                "Managed task is missing: $($definition.Name). " +
                "Run .\rr add after changing configuration."
            )
        }
        $trigger = $task.Triggers | Select-Object -First 1
        $actualStart = ([datetime]$trigger.StartBoundary).ToString(
            "HH:mm:ss"
        )
        $action = $task.Actions | Select-Object -First 1
        $expectedArguments = (
            '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f `
                $definition.Wrapper
        )
        $expectedPowerShell = Get-ReverseRepoPowerShell
        if (
            $actualStart -ne $definition.StartAt `
            -or [string]$action.Execute -ine $expectedPowerShell `
            -or [string]$action.WorkingDirectory -ne $repoRoot `
            -or [string]$action.Arguments -ne $expectedArguments
        ) {
            throw (
                "Managed task does not match current configuration: " +
                "$($definition.Name). Run .\rr add, review .\rr stat, " +
                "then enable again."
            )
        }
    }
}

function Assert-LiveEnableGate {
    $pythonPath = Get-ReverseRepoPython
    $gateScript = Join-Path `
        $PSScriptRoot `
        "verify_repo_release_gate.py"
    $qmtPath = Get-ReverseRepoLiveQmtPath
    $bindingPath = Join-Path `
        $repoRoot `
        "config\repo_live_account_binding.local.json"
    $liveChannelCertificatePath = Join-Path `
        $repoRoot `
        "reports\gc001_intraday\live_channel_validation\latest.json"
    $signingKeyPath = Join-Path `
        $repoRoot `
        "config\repo_release_gate_secret.local.json"
    $strategyConfigPath = Join-Path `
        $repoRoot `
        "config\runtime.local.json"
    foreach ($requiredPath in @(
        $pythonPath,
        $gateScript,
        $bindingPath,
        $liveChannelCertificatePath,
        $signingKeyPath,
        $strategyConfigPath
    )) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Live enable gate file is missing: $requiredPath"
        }
    }
    $gateArguments = @(
        $gateScript,
        "--qmt-path", $qmtPath,
        "--account-binding", $bindingPath,
        "--live-channel-certificate", $liveChannelCertificatePath,
        "--signing-key", $signingKeyPath,
        "--strategy-config", $strategyConfigPath
    )
    & $pythonPath @gateArguments
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Live enable gate failed."
    }
}

function Invoke-LiveChannelCertification {
    $scriptPath = Join-Path $PSScriptRoot "run_repo_live_channel_validation.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "快速实盘认证脚本不存在：$scriptPath"
    }
    if ([string]::IsNullOrWhiteSpace($LiveCertConfirmation)) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath
    }
    else {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
            -Confirmation $LiveCertConfirmation
    }
    exit $LASTEXITCODE
}

function Invoke-LiveChannelCertificationPreflight {
    $scriptPath = Join-Path $PSScriptRoot "run_repo_live_channel_validation.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Live-channel certification script is missing: $scriptPath"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -PreflightOnly
    exit $LASTEXITCODE
}

function Get-LiveChannelCertificationStatus {
    $pythonPath = Get-ReverseRepoPython
    $validatorPath = Join-Path $PSScriptRoot "repo_live_channel_validation.py"
    $certificatePath = Join-Path $repoRoot "reports\gc001_intraday\live_channel_validation\latest.json"
    if (-not (Test-Path -LiteralPath $certificatePath -PathType Leaf)) {
        Write-Output "实盘通道认证：不存在。"
        return
    }
    & $pythonPath $validatorPath status `
        --qmt-path (Get-ReverseRepoLiveQmtPath) `
        --account-binding (Join-Path $repoRoot "config\repo_live_account_binding.local.json") `
        --certificate $certificatePath `
        --signing-key (Join-Path $repoRoot "config\repo_release_gate_secret.local.json")
}

function Reset-LiveChannelCertificate {
    foreach ($name in $managedTaskNames) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -ne $task -and [string]$task.State -ne "Disabled") {
            throw "请先执行 .\rr off；实盘任务仍未禁用：$name"
        }
    }
    $mutexPath = Join-Path $repoRoot "reports\gc001_intraday\reverse_repo_execution.lock"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $mutexPath) |
        Out-Null
    try {
        $mutexProbe = [System.IO.File]::Open(
            $mutexPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        $mutexProbe.Dispose()
    }
    catch {
        throw "Global reverse-repo mutex is busy; reset is refused."
    }
    $directory = Join-Path $repoRoot "reports\gc001_intraday\live_channel_validation"
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        Remove-ReverseRepoLiveEnableManifest
        Write-Output "快速实盘认证证据不存在；实盘启用快照已撤销。"
        return
    }
    $items = Get-ChildItem -LiteralPath $directory -File -ErrorAction SilentlyContinue
    if ($items.Count -eq 0) {
        Remove-ReverseRepoLiveEnableManifest
        Write-Output "快速实盘认证证据不存在；实盘启用快照已撤销。"
        return
    }
    $archive = Join-Path $directory ("revoked\" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    if (-not $PSCmdlet.ShouldProcess($directory, "Archive and revoke live-channel certification")) {
        return
    }
    Remove-ReverseRepoLiveEnableManifest
    New-Item -ItemType Directory -Force -Path $archive | Out-Null
    foreach ($item in $items) {
        Move-Item -LiteralPath $item.FullName -Destination $archive
    }
    Write-Output "快速实盘证书及证据已撤销并归档：$archive"
}

function Configure-FailureEmail {
    $scriptPath = Join-Path `
        $PSScriptRoot `
        "configure_repo_failure_email.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Failure-email configuration script is missing: $scriptPath"
    }
    & $scriptPath
}

function Test-FailureEmail {
    $pythonPath = Get-ReverseRepoPython
    $alertScript = Join-Path $PSScriptRoot "repo_failure_alert.py"
    $alertConfigPath = Join-Path `
        $repoRoot `
        "config\repo_failure_email.local.json"
    $alertSecretPath = Join-Path `
        $repoRoot `
        "config\repo_failure_email_secret.local.clixml"
    foreach ($requiredPath in @(
        $pythonPath,
        $alertScript,
        $alertConfigPath,
        $alertSecretPath
    )) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Failure-email test file is missing: $requiredPath"
        }
    }
    $securePassword = Import-Clixml -LiteralPath $alertSecretPath
    if ($securePassword -isnot [securestring]) {
        throw "Failure-alert secret is not a Windows SecureString."
    }
    $credential = [pscredential]::new("smtp", $securePassword)
    $env:MINIQMT_ALERT_SMTP_PASSWORD = (
        $credential.GetNetworkCredential().Password
    )
    try {
        & $pythonPath `
            $alertScript `
            "--config" `
            $alertConfigPath `
            "--test-send"
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "Failure-email test failed."
        }
    }
    finally {
        Remove-Item Env:MINIQMT_ALERT_SMTP_PASSWORD `
            -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Developer-only simulation validation and stress-test orchestration. These
# functions are reachable only through the .\rr dev command group and never
# through the ordinary user surface. The live-enable gate stays live-only.
# ---------------------------------------------------------------------------

function Reset-SimulationCertificate {
    $certificatePath = Join-Path `
        $repoRoot `
        "reports\gc001_intraday\simulation_validation\latest.json"
    if (-not (Test-Path -LiteralPath $certificatePath -PathType Leaf)) {
        Write-Output "模拟验证证书不存在。"
        return
    }
    $certificate = Get-Item -LiteralPath $certificatePath
    $archiveDirectory = Join-Path $certificate.DirectoryName "revoked"
    $archiveName = "latest_revoked_{0}.json" -f (
        Get-Date -Format "yyyyMMdd_HHmmss"
    )
    $archivePath = Join-Path $archiveDirectory $archiveName
    if (-not $PSCmdlet.ShouldProcess(
        $certificate.FullName,
        "Archive and revoke simulation validation certificate"
    )) {
        return
    }
    New-Item `
        -ItemType Directory `
        -Force `
        -Path $archiveDirectory |
        Out-Null
    Move-Item `
        -LiteralPath $certificate.FullName `
        -Destination $archivePath
    Write-Output "模拟验证证书已撤销并归档：$archivePath"
}

function Install-SimulationCertificationTasks {
    foreach ($name in $managedTaskNames) {
        $liveTask = Get-ScheduledTask `
            -TaskName $name `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $liveTask `
            -and [string]$liveTask.State -ne "Disabled"
        ) {
            throw (
                "Live reverse-repo task is enabled: $name. " +
                "Run .\rr off before scheduling developer simulation " +
                "certification."
            )
        }
    }
    $scriptPath = Join-Path `
        $PSScriptRoot `
        "install_repo_simulation_validation_tasks.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Developer simulation certification installer is missing: $scriptPath"
    }
    if ([string]::IsNullOrWhiteSpace($CertDate)) {
        & $scriptPath
        return
    }
    try {
        $parsedDate = [datetime]::ParseExact(
            $CertDate,
            "yyyy-MM-dd",
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch [FormatException] {
        throw "Developer simulation certification date must use YYYY-MM-DD format."
    }
    & $scriptPath -ValidationDate $parsedDate
}

function Get-SimulationCertificationTaskStatus {
    foreach ($name in $certTaskNames) {
        $task = Get-ScheduledTask `
            -TaskName $name `
            -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            [pscustomobject]@{
                TaskName = $name
                Installed = $false
                State = "NotInstalled"
                Trigger = $null
                NextRunTime = $null
                LastRunTime = $null
                LastResult = $null
            }
            continue
        }
        $info = Get-ScheduledTaskInfo -TaskName $name
        $isDisabled = ([string]$task.State -eq "Disabled")
        $hasNeverRun = (
            [int64]$info.LastTaskResult -eq 267011 `
            -or $info.LastRunTime.Year -lt 2000
        )
        [pscustomobject]@{
            TaskName = $name
            Installed = $true
            State = [string]$task.State
            Trigger = $task.Triggers[0].StartBoundary
            NextRunTime = if ($isDisabled) {
                "已撤销，不会运行"
            }
            elseif ($info.NextRunTime -le [datetime]::MinValue) {
                "没有后续计划"
            }
            else {
                $info.NextRunTime
            }
            LastRunTime = if ($hasNeverRun) {
                "尚未运行"
            }
            else {
                $info.LastRunTime
            }
            LastResult = Format-TaskResult `
                -Result ([int64]$info.LastTaskResult)
        }
    }
}

function Disable-SimulationCertificationTasks {
    $installed = @(
        foreach ($name in $certTaskNames) {
            $task = Get-ScheduledTask `
                -TaskName $name `
                -ErrorAction SilentlyContinue
            if ($null -ne $task) {
                $task
            }
        }
    )
    if ($installed.Count -eq 0) {
        Write-Output "模拟验证认证任务未安装，无需撤销。"
        return
    }
    $running = @(
        $installed | Where-Object { [string]$_.State -eq "Running" }
    )
    if ($running.Count -gt 0) {
        throw (
            "A developer simulation certification task is already running. " +
            "Refusing abrupt termination."
        )
    }
    foreach ($task in $installed) {
        if ($PSCmdlet.ShouldProcess(
            $task.TaskName,
            "Disable developer simulation certification task"
        )) {
            Disable-ScheduledTask -TaskName $task.TaskName | Out-Null
        }
    }
    Get-SimulationCertificationTaskStatus
}

function Remove-SimulationCertificationTasks {
    $installed = @(
        foreach ($name in $certTaskNames) {
            $task = Get-ScheduledTask `
                -TaskName $name `
                -ErrorAction SilentlyContinue
            if ($null -ne $task) {
                $task
            }
        }
    )
    if ($installed.Count -eq 0) {
        Write-Output "模拟验证认证任务未安装，无需删除。"
        return
    }
    $running = @(
        $installed | Where-Object { [string]$_.State -eq "Running" }
    )
    if ($running.Count -gt 0) {
        throw (
            "A developer simulation certification task is already running. " +
            "Refusing abrupt termination."
        )
    }
    foreach ($task in $installed) {
        if ($PSCmdlet.ShouldProcess(
            $task.TaskName,
            "Delete developer simulation certification task"
        )) {
            Unregister-ScheduledTask `
                -TaskName $task.TaskName `
                -Confirm:$false
        }
    }
    Get-SimulationCertificationTaskStatus
}

function Install-SimulationStressTask {
    $scriptPath = Join-Path `
        $PSScriptRoot `
        "install_repo_simulation_stress_task.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Developer simulation stress installer is missing: $scriptPath"
    }
    if ([string]::IsNullOrWhiteSpace($StressDate)) {
        & $scriptPath
        return
    }
    try {
        $parsedDate = [datetime]::ParseExact(
            $StressDate,
            "yyyy-MM-dd",
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch [FormatException] {
        throw "Developer simulation stress date must use YYYY-MM-DD format."
    }
    & $scriptPath -StressDate $parsedDate
}

function Get-SimulationStressTaskStatus {
    $task = Get-ScheduledTask `
        -TaskName $stressTaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return [pscustomobject]@{
            TaskName = $stressTaskName
            Installed = $false
            State = "NotInstalled"
            Trigger = $null
            NextRunTime = $null
            LastRunTime = $null
            LastResult = $null
        }
    }
    $info = Get-ScheduledTaskInfo -TaskName $stressTaskName
    $isDisabled = ([string]$task.State -eq "Disabled")
    $hasNeverRun = (
        [int64]$info.LastTaskResult -eq 267011 `
        -or $info.LastRunTime.Year -lt 2000
    )
    return [pscustomobject]@{
        TaskName = $stressTaskName
        Installed = $true
        State = [string]$task.State
        Trigger = $task.Triggers[0].StartBoundary
        NextRunTime = if ($isDisabled) {
            "已撤销，不会运行"
        }
        elseif ($info.NextRunTime -le [datetime]::MinValue) {
            "没有后续计划"
        }
        else {
            $info.NextRunTime
        }
        LastRunTime = if ($hasNeverRun) {
            "尚未运行"
        }
        else {
            $info.LastRunTime
        }
        LastResult = Format-TaskResult `
            -Result ([int64]$info.LastTaskResult)
    }
}

function Disable-SimulationStressTask {
    $task = Get-ScheduledTask `
        -TaskName $stressTaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Output "模拟压力测试任务未安装，无需撤销。"
        return
    }
    if ([string]$task.State -eq "Running") {
        throw (
            "Developer simulation stress task is already running. Refusing " +
            "abrupt termination because simulated orders or positions may " +
            "need cleanup."
        )
    }
    if ($PSCmdlet.ShouldProcess(
        $stressTaskName,
        "Disable developer simulation stress task"
    )) {
        Disable-ScheduledTask -TaskName $stressTaskName | Out-Null
    }
    Get-SimulationStressTaskStatus
}

function Remove-SimulationStressTask {
    $task = Get-ScheduledTask `
        -TaskName $stressTaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Output "模拟压力测试任务未安装，无需删除。"
        return
    }
    if ([string]$task.State -eq "Running") {
        throw (
            "Developer simulation stress task is already running. Refusing " +
            "abrupt termination because simulated orders or positions may " +
            "need cleanup."
        )
    }
    if ($PSCmdlet.ShouldProcess(
        $stressTaskName,
        "Delete developer simulation stress task"
    )) {
        Unregister-ScheduledTask `
            -TaskName $stressTaskName `
            -Confirm:$false
    }
    Get-SimulationStressTaskStatus
}

function Get-DeveloperValidationStatus {
    $certificatePath = Join-Path `
        $repoRoot `
        "reports\gc001_intraday\simulation_validation\latest.json"
    if (-not (Test-Path -LiteralPath $certificatePath -PathType Leaf)) {
        Write-Output "模拟验证证书：不存在。执行 .\rr dev cert [日期]。"
    }
    else {
        $pythonPath = Get-ReverseRepoPython
        $validatorPath = Join-Path `
            $PSScriptRoot `
            "dev_simulation_certificate.py"
        $signingKeyPath = Join-Path `
            $repoRoot `
            "config\repo_release_gate_secret.local.json"
        & $pythonPath `
            $validatorPath `
            "--certificate" `
            $certificatePath `
            "--signing-key" `
            $signingKeyPath
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            Write-Output "模拟验证证书：与当前代码不匹配或无效。"
        }
        else {
            Write-Output "模拟验证证书：与当前代码匹配。"
        }
    }
    Get-SimulationCertificationTaskStatus
    Write-Output ""
    Get-SimulationStressTaskStatus
}

function Connect-DeveloperSimulationBinding {
    $scriptPath = Join-Path `
        $PSScriptRoot `
        "dev_bind_simulation.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Developer simulation binding script is missing: $scriptPath"
    }
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $scriptPath
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Developer simulation binding failed."
    }
}

function Show-ReverseRepoTaskHelp {
    @"
rr - miniQMT 逆回购自动任务管理工具

【首次初始化】
  .\rr init
      在仓库内安装Python 3.12.10 x64、创建.venv、使用国内镜像安装XtQuant，
      生成本机配置和签名密钥，并引导账户绑定；不会启用实盘任务。

【实盘任务：关键命令】
  .\rr stat
      首先查看两项实盘任务、策略参数、调度时间和最近结果。

  .\rr off
      撤销实盘启用快照并禁用两个任务，但保留任务定义。

  .\rr on
      通过本地验证、账户绑定和1000元实盘快速证书门禁后创建签名启用快照，
      仅启用资金比例大于0的任务。任务尚未安装时先执行 .\rr add。

  .\rr add
      安装或更新两个实盘任务；安装完成后保持Disabled，不会立即交易。

  .\rr del
      删除两个实盘任务，不删除代码、日志、报告或只读任务。

  .\rr clear
      禁用并删除本项目全部已知计划任务，包括实盘、只读检查、模拟认证、
      恢复和压力测试旧任务，同时撤销实盘启用快照；不删除代码、配置、
      账户绑定、证书或报告。运行中的任务不会被强制终止。

  当前实盘参数：
    第一次：$firstStartText 启动，$firstExecutionText 执行GC001，
            使用 $firstCashUsagePercent 可用资金，最多尝试5分钟。
    第二次：$secondStartText 启动，$secondExecutionText 扫描GC001/R-001，
            使用 $secondCashUsagePercent 的首次有效资金快照。

【快速实盘通道认证：固定1000元】
  .\rr cert
      前台执行一次GC001真实逆回购，累计成交本金硬上限1000元。必须先rr off，
      并人工输入LIVE 1000；成功后任务仍保持Disabled，不会自动rr on。

  .\rr cert stat
      只读核验证书、journal和本机环境；不连接QMT、不下单。

  .\rr cert reset
      归档并撤销快速实盘证书及其证据，同时撤销实盘启用快照。

【邮件与帮助】

  .\rr mail
      可选：配置执行结果/认证通知邮箱。SMTP 密码由 Windows 当前用户加密
      保存，不写入代码、日志或版本库；未配置不会阻止任务启用或策略执行。

  .\rr mt
      使用已保存的加密配置发送一封测试邮件，不重新输入密码。

  .\rr help
      显示本帮助。直接运行 .\rr 也会显示本帮助。

【开发者模拟验证与压力测试（普通用户无需使用）】
  .\rr dev bind
      配置模拟miniQMT路径并绑定模拟账户；开发者在本地验证代码用。

  .\rr dev cert [YYYY-MM-DD]
      部署单日模拟认证：正式上午/下午执行器验证正常路径，另一隔离窗口
      验证崩溃恢复，当日15:31签发模拟验证证书。该证书不参与实盘启用门禁。

  .\rr dev cert stat | off | del | reset
      查看、撤销、删除模拟认证任务，或归档并撤销模拟验证证书。

  .\rr dev stress [YYYY-MM-DD]
      部署模拟账户5Hz全链路压力测试；stat/off/del 查看、撤销、删除。

  .\rr dev status
      汇总查看模拟验证证书、认证任务和压力任务状态。

  详细说明见 docs\developer_validation.md。

运行要求：
  - 可在 reverse_repo 目录执行；仓库根目录的 rr 是兼容转发入口。
  - 机器相关路径配置在 config\runtime.local.json，不写入版本库。
  - Windows 用户需要保持登录。
  - 交易日需要启动并登录 miniQMT 客户端。
  - 脚本运行时查询账户，并与本机非版本控制的哈希绑定核对；
    代码、日志和公开仓库中不保存证券账号。
"@ | Write-Output
}

switch ($Action) {
    "Initialize" {
        $powerShellPath = Get-ReverseRepoPowerShell
        & $powerShellPath `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File (Join-Path $PSScriptRoot "initialize_reverse_repo.ps1")
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "Reverse-repo initialization failed."
        }
    }
    "Install" {
        Install-ManagedTasks
        Get-ManagedTaskStatus
    }
    "Remove" {
        Remove-ManagedTasks
        Write-Output "逆回购计划任务已删除。"
    }
    "Clear" {
        Clear-AllReverseRepoTasks
    }
    "Status" {
        Get-ManagedTaskStatus
        Write-Output ""
        try {
            Assert-LiveEnableGate
        }
        catch {
            Write-Output "认证依据：当前没有可用于rr on的有效证书。"
            Write-Output "原因：$($_.Exception.Message)"
        }
    }
    "Enable" {
        Set-ManagedTasksEnabled -Enabled $true
        Get-ManagedTaskStatus
    }
    "Disable" {
        Set-ManagedTasksEnabled -Enabled $false
        Get-ScheduledTask `
            -TaskName $managedTaskNames `
            -ErrorAction SilentlyContinue |
            Select-Object TaskName, State
    }
    "ConfigureMail" {
        Configure-FailureEmail
    }
    "TestMail" {
        Test-FailureEmail
    }
    "LiveCert" {
        Invoke-LiveChannelCertification
    }
    "LiveCertPreflight" {
        Invoke-LiveChannelCertificationPreflight
    }
    "LiveCertStatus" {
        Get-LiveChannelCertificationStatus
    }
    "LiveCertReset" {
        Reset-LiveChannelCertificate
    }
    "DevBind" {
        Connect-DeveloperSimulationBinding
    }
    "DevStatus" {
        Get-DeveloperValidationStatus
    }
    "DevCert" {
        Install-SimulationCertificationTasks
    }
    "DevCertDisable" {
        Disable-SimulationCertificationTasks
    }
    "DevCertRemove" {
        Remove-SimulationCertificationTasks
    }
    "DevCertStatus" {
        Get-SimulationCertificationTaskStatus
    }
    "DevCertReset" {
        Reset-SimulationCertificate
    }
    "DevStress" {
        Install-SimulationStressTask
    }
    "DevStressDisable" {
        Disable-SimulationStressTask
    }
    "DevStressRemove" {
        Remove-SimulationStressTask
    }
    "DevStressStatus" {
        Get-SimulationStressTaskStatus
    }
    "Help" {
        $runtimeConfigPath = Join-Path `
            $repoRoot `
            "config\runtime.local.json"
        if (Test-Path -LiteralPath $runtimeConfigPath -PathType Leaf) {
            Initialize-TaskDefinitions
        }
        Show-ReverseRepoTaskHelp
    }
}
