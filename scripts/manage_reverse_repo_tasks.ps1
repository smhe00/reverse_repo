[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "Install",
        "Remove",
        "Status",
        "Enable",
        "Disable",
        "ConfigureMail",
        "TestMail",
        "ResetCertificate",
        "Stress",
        "StressDisable",
        "StressRemove",
        "StressStatus",
        "Help"
    )]
    [string]$Action,
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
$obsoleteTaskNames = @(
    "miniQMT Reverse Repo Once",
    "miniQMT GC001 Daily 90pct 093042",
    "miniQMT GC001 R001 Afternoon Sweep",
    "miniQMT Reverse Repo Morning",
    "miniQMT Reverse Repo Afternoon"
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

function Get-PowerShell7Path {
    $command = Get-Command "pwsh.exe" -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "PowerShell 7 (pwsh.exe) is required."
    }
    return $command.Source
}

function Get-ManagedTaskStatus {
    Initialize-TaskDefinitions
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
        $actualStart = ([datetime]$trigger.StartBoundary).ToString(
            "HH:mm:ss"
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
                $actualStart -eq $definition.StartAt
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
    if ($PSCmdlet.ShouldProcess(
        (Get-ReverseRepoLiveEnableManifestPath),
        "Revoke live-enable snapshot before task installation"
    )) {
        Remove-ReverseRepoLiveEnableManifest
    }
    $pwshPath = Get-PowerShell7Path
    $userId = (
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    )
    foreach ($definition in $taskDefinitions) {
        if (-not (Test-Path -LiteralPath $definition.Wrapper)) {
            throw "Task wrapper does not exist: $($definition.Wrapper)"
        }
        if (-not $PSCmdlet.ShouldProcess(
            $definition.Name,
            "Install or update Windows scheduled task"
        )) {
            continue
        }
        $taskAction = New-ScheduledTaskAction `
            -Execute $pwshPath `
            -Argument (
                '-NoProfile -File "{0}"' -f $definition.Wrapper
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

function Set-ManagedTasksEnabled {
    param([Parameter(Mandatory = $true)][bool]$Enabled)
    $manifestCreated = $false
    if ($Enabled) {
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
        $expectedArguments = '-NoProfile -File "{0}"' -f (
            $definition.Wrapper
        )
        if (
            $actualStart -ne $definition.StartAt `
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
    $qmtPath = Get-ReverseRepoQmtPath -Environment "live"
    $bindingPath = Join-Path `
        $repoRoot `
        "config\repo_live_account_binding.local.json"
    $certificatePath = Join-Path `
        $repoRoot `
        "reports\gc001_intraday\simulation_validation\latest.json"
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
        $certificatePath,
        $signingKeyPath,
        $strategyConfigPath
    )) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Live enable gate file is missing: $requiredPath"
        }
    }
    & $pythonPath `
        $gateScript `
        "--qmt-path" `
        $qmtPath `
        "--account-binding" `
        $bindingPath `
        "--simulation-certificate" `
        $certificatePath `
        "--signing-key" `
        $signingKeyPath `
        "--strategy-config" `
        $strategyConfigPath
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Live enable gate failed."
    }
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

function Reset-SimulationCertificate {
    $certificatePath = Join-Path `
        $repoRoot `
        "reports\gc001_intraday\simulation_validation\latest.json"
    if (-not (Test-Path -LiteralPath $certificatePath -PathType Leaf)) {
        Remove-ReverseRepoLiveEnableManifest
        Write-Output "模拟证书不存在，实盘门禁已经处于未认证状态。"
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
        "Archive and revoke simulation certificate"
    )) {
        return
    }
    Remove-ReverseRepoLiveEnableManifest
    New-Item `
        -ItemType Directory `
        -Force `
        -Path $archiveDirectory |
        Out-Null
    Move-Item `
        -LiteralPath $certificate.FullName `
        -Destination $archivePath
    Write-Output "模拟证书已撤销并归档：$archivePath"
}

function Install-SimulationStressTask {
    $scriptPath = Join-Path `
        $PSScriptRoot `
        "install_repo_simulation_stress_task.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Simulation stress installer is missing: $scriptPath"
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
        throw "Stress date must use YYYY-MM-DD format."
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
            "Simulation stress task is already running. Refusing abrupt " +
            "termination because simulated orders or positions may need cleanup."
        )
    }
    if ($PSCmdlet.ShouldProcess(
        $stressTaskName,
        "Disable one-time simulation stress task"
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
            "Simulation stress task is already running. Refusing abrupt " +
            "termination because simulated orders or positions may need cleanup."
        )
    }
    if ($PSCmdlet.ShouldProcess(
        $stressTaskName,
        "Delete one-time simulation stress task"
    )) {
        Unregister-ScheduledTask `
            -TaskName $stressTaskName `
            -Confirm:$false
    }
    Get-SimulationStressTaskStatus
}

function Show-ReverseRepoTaskHelp {
    @"
rr - miniQMT 逆回购自动任务管理工具

用途：
  管理以下两个 Windows 任务计划程序任务：

  1. 第一次 GC001 策略
     $firstStartText 启动，$firstExecutionText 按实时买一借出
     可用资金的 $firstCashUsagePercent，最长尝试5分钟或至当前交易时段结束；
     比例为0时该任务保持禁用。

  2. 第二次 GC001 / R-001 兜底策略
     $secondStartText 启动，$secondExecutionText 查询实时剩余资金，按整笔五档预计
     成交年化择优借出 GC001 或 R-001，目标为首次有效资金快照的
     $secondCashUsagePercent；比例为0时该任务保持禁用。

命令：
  .\rr add
      安装或更新上述两个任务；安装完成后保持禁用。
      可重复执行；不会产生当天委托。
      如果存在旧版固定参数任务，会先禁用并删除，避免重复执行。

  .\rr del
      从 Windows 任务计划程序删除上述两个新任务。
      不删除策略代码、日志、报告，也不删除其他 Windows 任务。

  .\rr stat
      显示任务是否安装、当前状态、计划时间、下一次运行时间、
      最近运行时间及最近返回结果。

  .\rr on
      通过形式验证、账户绑定和模拟能力证书门禁后，为当前时间与资金比例
      创建签名启用快照，仅启用资金比例大于0的任务。如果任务尚未安装，
      请先执行 .\rr add。

  .\rr off
      撤销启用快照并暂停两个任务，但保留任务配置；之后可用 .\rr on 恢复。

  .\rr mail
      可选：配置故障告警邮箱。SMTP 密码由 Windows 当前用户加密保存，
      不写入代码、日志或版本库；未配置不会阻止任务启用或策略执行。

  .\rr mt
      使用已保存的加密配置发送一封测试邮件，不重新输入密码。

  .\rr reset
      手动撤销模拟证书并移入本机归档目录。撤销后必须重新完成
      模拟验证，才能再次启用实盘任务。

  .\rr stress [YYYY-MM-DD]
      部署一次性的5Hz全链路压力测试任务。省略日期时选择下一个可完整
      执行的工作日；指定日期仅接受YYYY-MM-DD。任务固定使用模拟miniQMT
      路径和模拟账户绑定，任何环境或账户核验失败都会在下单前停止。

  .\rr stress stat | off | del
      stat查看一次性任务；off在执行前撤销调度但保留任务；del彻底删除。
      已经运行时拒绝强制终止，避免跳过模拟委托撤销和持仓清理。

  .\rr help
      显示本帮助。直接运行 .\rr 也会显示本帮助。

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
    "Install" {
        Install-ManagedTasks
        Get-ManagedTaskStatus
    }
    "Remove" {
        Remove-ManagedTasks
        Write-Output "逆回购计划任务已删除。"
    }
    "Status" {
        Get-ManagedTaskStatus
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
    "ResetCertificate" {
        Reset-SimulationCertificate
    }
    "Stress" {
        Install-SimulationStressTask
    }
    "StressDisable" {
        Disable-SimulationStressTask
    }
    "StressRemove" {
        Remove-SimulationStressTask
    }
    "StressStatus" {
        Get-SimulationStressTaskStatus
    }
    "Help" {
        Initialize-TaskDefinitions
        Show-ReverseRepoTaskHelp
    }
}
