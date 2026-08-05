[CmdletBinding()]
param(
    [string]$FirstExecutionTime = "",
    [string]$FirstCashUsageRatio = "",
    [string]$SecondExecutionTime = "",
    [string]$SecondCashUsageRatio = "",
    [switch]$NonInteractiveConfirmed
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")

$repoRoot = Get-ReverseRepoRoot
$configPath = Join-Path $repoRoot "config\runtime.local.json"
$exampleConfigPath = Join-Path $repoRoot "config\runtime.example.json"
$verifyPath = Join-Path $repoRoot "verify.ps1"
$liveTaskNames = @(
    "miniQMT Reverse Repo First",
    "miniQMT Reverse Repo Second",
    "miniQMT Reverse Repo Once",
    "miniQMT GC001 Daily 90pct 093042",
    "miniQMT GC001 R001 Afternoon Sweep",
    "miniQMT Reverse Repo Morning",
    "miniQMT Reverse Repo Afternoon"
)

function Assert-LiveStrategyIsOff {
    $unsafe = @()
    foreach ($taskName in $liveTaskNames) {
        $task = Get-ScheduledTask `
            -TaskName $taskName `
            -ErrorAction SilentlyContinue
        if ($null -ne $task -and [string]$task.State -ne "Disabled") {
            $unsafe += "$taskName=$($task.State)"
        }
    }
    if ($unsafe.Count -ne 0) {
        throw (
            "Live strategy is not off. Run .\rr off first. Active tasks: " +
            ($unsafe -join ", ")
        )
    }
    $manifestPath = Get-ReverseRepoLiveEnableManifestPath
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        throw (
            "Live-enable snapshot still exists. Run .\rr off first: " +
            $manifestPath
        )
    }
}

function Read-ValueOrDefault {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Allowed,
        [Parameter(Mandatory = $true)][string]$Current,
        [Parameter(Mandatory = $true)][string]$Default
    )
    $prompt = @(
        "",
        $Title,
        "  Allowed : $Allowed",
        "  Current : $Current",
        "  Default : $Default  (enter D)",
        "  Actions : Enter=keep current | D=use default | Q=cancel",
        "Input"
    ) -join [Environment]::NewLine
    $value = Read-Host $prompt
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Current
    }
    $trimmed = $value.Trim()
    if ($trimmed -ieq "Q") {
        throw [OperationCanceledException]::new(
            "Strategy parameter change cancelled by user."
        )
    }
    if ($trimmed -ieq "D") {
        return $Default
    }
    return $trimmed
}

function ConvertTo-InvariantRatio {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $parsed = 0.0
    $valid = [double]::TryParse(
        $Value,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed
    )
    if (
        -not $valid `
        -or [double]::IsNaN($parsed) `
        -or [double]::IsInfinity($parsed) `
        -or $parsed -lt 0 `
        -or $parsed -gt 1
    ) {
        throw "$Name must be a number from 0 through 1."
    }
    return $parsed
}

function Read-ValidatedRatio {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Allowed,
        [Parameter(Mandatory = $true)][string]$Current,
        [Parameter(Mandatory = $true)][string]$Default,
        [Parameter(Mandatory = $true)][string]$Name
    )
    while ($true) {
        $value = Read-ValueOrDefault `
            -Title $Title `
            -Allowed $Allowed `
            -Current $Current `
            -Default $Default
        try {
            return ConvertTo-InvariantRatio -Value $value -Name $Name
        }
        catch [OperationCanceledException] {
            throw
        }
        catch [ArgumentException] {
            Write-Warning $_.Exception.Message
        }
        catch [FormatException] {
            Write-Warning $_.Exception.Message
        }
        catch [System.Management.Automation.RuntimeException] {
            Write-Warning $_.Exception.Message
        }
    }
}

function Read-ValidatedTime {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Allowed,
        [Parameter(Mandatory = $true)][string]$Current,
        [Parameter(Mandatory = $true)][string]$Default,
        [Parameter(Mandatory = $true)]
        [ValidateSet("first", "second")]
        [string]$Stage,
        [TimeSpan]$FirstTime = [TimeSpan]::Zero
    )
    while ($true) {
        $value = Read-ValueOrDefault `
            -Title $Title `
            -Allowed $Allowed `
            -Current $Current `
            -Default $Default
        try {
            return ConvertTo-ValidatedTime `
                -Value $value `
                -Stage $Stage `
                -FirstTime $FirstTime
        }
        catch [OperationCanceledException] {
            throw
        }
        catch {
            Write-Warning $_.Exception.Message
        }
    }
}

function ConvertTo-ValidatedTime {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)]
        [ValidateSet("first", "second")]
        [string]$Stage,
        [TimeSpan]$FirstTime = [TimeSpan]::Zero
    )
        $parsed = [TimeSpan]::Zero
        $validFormat = [TimeSpan]::TryParseExact(
            $Value,
            "hh\:mm\:ss",
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsed
        )
        if (-not $validFormat) {
            throw "Time must use exact HH:mm:ss format."
        }
        $morningStart = [TimeSpan]::FromHours(9.5)
        $afternoonStart = [TimeSpan]::FromHours(13)
        if ($Stage -eq "first") {
            $morningEnd = [TimeSpan]::FromHours(11) +
                [TimeSpan]::FromMinutes(28)
            $afternoonEnd = [TimeSpan]::FromHours(15) +
                [TimeSpan]::FromMinutes(28)
            $inWindow = (
                ($parsed -ge $morningStart -and $parsed -le $morningEnd) `
                -or (
                    $parsed -ge $afternoonStart `
                    -and $parsed -le $afternoonEnd
                )
            )
            if (-not $inWindow) {
                throw (
                    "First time must be 09:30:00..11:28:00 or " +
                    "13:00:00..15:28:00."
                )
            }
        }
        else {
            $morningEnd = [TimeSpan]::FromHours(11.5)
            $afternoonEnd = [TimeSpan]::FromHours(15.5)
            $inWindow = (
                ($parsed -ge $morningStart -and $parsed -lt $morningEnd) `
                -or (
                    $parsed -ge $afternoonStart `
                    -and $parsed -lt $afternoonEnd
                )
            )
            if (-not $inWindow) {
                throw (
                    "Second time must be 09:30:00..<11:30:00 or " +
                    "13:00:00..<15:30:00."
                )
            }
            if (($parsed - $FirstTime) -lt [TimeSpan]::FromMinutes(5)) {
                throw (
                    "Second time must be at least five minutes after " +
                    "the first time."
                )
            }
        }
        return [pscustomobject]@{
            Text = ([datetime]::Today.Add($parsed).ToString("HH:mm:ss"))
            Value = $parsed
        }
}

function Set-JsonProperty {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][object]$Value
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        $Object | Add-Member `
            -MemberType NoteProperty `
            -Name $Name `
            -Value $Value
    }
    else {
        $property.Value = $Value
    }
}

function Set-CurrentAndLegacyProperty {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$LegacyName,
        [Parameter(Mandatory = $true)][object]$Value
    )
    Set-JsonProperty -Object $Object -Name $Name -Value $Value
    if ($null -ne $Object.PSObject.Properties[$LegacyName]) {
        Set-JsonProperty `
            -Object $Object `
            -Name $LegacyName `
            -Value $Value
    }
}

function Write-BytesAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes
    )
    $directory = Split-Path -Parent $Path
    $leaf = Split-Path -Leaf $Path
    $suffix = [guid]::NewGuid().ToString("N")
    $temporaryPath = Join-Path $directory ".$leaf.tmp.$suffix"
    $backupPath = Join-Path $directory ".$leaf.bak.$suffix"
    try {
        [System.IO.File]::WriteAllBytes($temporaryPath, $Bytes)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Replace(
                $temporaryPath,
                $Path,
                $backupPath,
                $true
            )
        }
        else {
            [System.IO.File]::Move($temporaryPath, $Path)
        }
    }
    finally {
        foreach ($cleanupPath in @($temporaryPath, $backupPath)) {
            if (Test-Path -LiteralPath $cleanupPath) {
                Remove-Item -LiteralPath $cleanupPath -Force
            }
        }
    }
}

function Write-ConfigAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Config
    )
    $json = ($Config | ConvertTo-Json -Depth 20) + `
        [Environment]::NewLine
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    Write-BytesAtomically -Path $Path -Bytes $utf8.GetBytes($json)
}

function Reset-RuntimeConfigCache {
    $script:ReverseRepoRuntimeConfig = $null
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Runtime configuration is missing. Run .\rr init first."
}
if (-not (Test-Path -LiteralPath $exampleConfigPath -PathType Leaf)) {
    throw "Default configuration sample is missing: $exampleConfigPath"
}
if (-not (Test-Path -LiteralPath $verifyPath -PathType Leaf)) {
    throw "Verification script is missing: $verifyPath"
}

Assert-LiveStrategyIsOff
$originalBytes = [System.IO.File]::ReadAllBytes($configPath)
$candidateActive = $false
$committed = $false

try {
    $current = Read-ReverseRepoJson -Path $configPath
    $currentFirstTime = Format-ReverseRepoClockTime `
        (Get-ReverseRepoFirstExecutionTime)
    $currentSecondTime = Format-ReverseRepoClockTime `
        (Get-ReverseRepoSecondExecutionTime)
    $currentFirstRatio = (
        Get-ReverseRepoFirstCashUsageRatio
    ).ToString("0.####", [Globalization.CultureInfo]::InvariantCulture)
    $currentSecondRatio = (
        Get-ReverseRepoSecondCashUsageRatio
    ).ToString("0.####", [Globalization.CultureInfo]::InvariantCulture)

    $defaults = Read-ReverseRepoJson -Path $exampleConfigPath
    $defaultFirstTimeResult = ConvertTo-ValidatedTime `
        -Value ([string]$defaults.first_execution_time) `
        -Stage "first"
    $defaultSecondTimeResult = ConvertTo-ValidatedTime `
        -Value ([string]$defaults.second_execution_time) `
        -Stage "second" `
        -FirstTime $defaultFirstTimeResult.Value
    $defaultFirstRatio = ConvertTo-InvariantRatio `
        -Value ([string]$defaults.first_cash_usage_ratio) `
        -Name "default first_cash_usage_ratio"
    $defaultSecondRatio = ConvertTo-InvariantRatio `
        -Value ([string]$defaults.second_cash_usage_ratio) `
        -Name "default second_cash_usage_ratio"
    $defaultFirstRatioText = $defaultFirstRatio.ToString(
        "0.####",
        [Globalization.CultureInfo]::InvariantCulture
    )
    $defaultSecondRatioText = $defaultSecondRatio.ToString(
        "0.####",
        [Globalization.CultureInfo]::InvariantCulture
    )

    $providedValues = @(
        $FirstExecutionTime,
        $FirstCashUsageRatio,
        $SecondExecutionTime,
        $SecondCashUsageRatio
    )
    if (
        -not $NonInteractiveConfirmed `
        -and @($providedValues | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_)
        }).Count -ne 0
    ) {
        throw (
            "Command-line strategy values require " +
            "-NonInteractiveConfirmed."
        )
    }
    Write-Output "Live tasks are confirmed Disabled."
    if ($NonInteractiveConfirmed) {
        if (@($providedValues | Where-Object {
            [string]::IsNullOrWhiteSpace($_)
        }).Count -ne 0) {
            throw "All four strategy values are required."
        }
        $firstTimeResult = ConvertTo-ValidatedTime `
            -Value $FirstExecutionTime `
            -Stage "first"
        $firstTime = $firstTimeResult.Text
        $firstRatio = ConvertTo-InvariantRatio `
            -Value $FirstCashUsageRatio `
            -Name "first_cash_usage_ratio"
        $secondTimeResult = ConvertTo-ValidatedTime `
            -Value $SecondExecutionTime `
            -Stage "second" `
            -FirstTime $firstTimeResult.Value
        $secondTime = $secondTimeResult.Text
        $secondRatio = ConvertTo-InvariantRatio `
            -Value $SecondCashUsageRatio `
            -Name "second_cash_usage_ratio"
        Write-Output (
            "Using four values explicitly confirmed by the local UI."
        )
    }
    else {
        Write-Output (
            "Defaults are validated values from runtime.example.json."
        )
        $firstTimeResult = Read-ValidatedTime `
            -Title "[1/4] First execution time" `
            -Allowed (
                "09:30:00..11:28:00 or 13:00:00..15:28:00; " +
                "exact HH:mm:ss"
            ) `
            -Current $currentFirstTime `
            -Default $defaultFirstTimeResult.Text `
            -Stage "first"
        $firstTime = $firstTimeResult.Text
        $firstRatio = Read-ValidatedRatio `
            -Title "[2/4] First cash usage ratio" `
            -Allowed "0..1 inclusive" `
            -Current $currentFirstRatio `
            -Default $defaultFirstRatioText `
            -Name "first_cash_usage_ratio"
        $secondTimeResult = Read-ValidatedTime `
            -Title "[3/4] Second execution time" `
            -Allowed (
                "09:30:00..<11:30:00 or 13:00:00..<15:30:00; " +
                "at least first+5m; exact HH:mm:ss"
            ) `
            -Current $currentSecondTime `
            -Default $defaultSecondTimeResult.Text `
            -Stage "second" `
            -FirstTime $firstTimeResult.Value
        $secondTime = $secondTimeResult.Text
        $secondRatio = Read-ValidatedRatio `
            -Title "[4/4] Second cash usage ratio" `
            -Allowed "0..1 inclusive" `
            -Current $currentSecondRatio `
            -Default $defaultSecondRatioText `
            -Name "second_cash_usage_ratio"
    }

    $candidate = $current | ConvertTo-Json -Depth 20 | ConvertFrom-Json
    Set-CurrentAndLegacyProperty `
        -Object $candidate `
        -Name "first_execution_time" `
        -LegacyName "morning_execution_time" `
        -Value $firstTime
    Set-CurrentAndLegacyProperty `
        -Object $candidate `
        -Name "first_cash_usage_ratio" `
        -LegacyName "morning_cash_usage_ratio" `
        -Value $firstRatio
    Set-CurrentAndLegacyProperty `
        -Object $candidate `
        -Name "second_execution_time" `
        -LegacyName "afternoon_execution_time" `
        -Value $secondTime
    Set-CurrentAndLegacyProperty `
        -Object $candidate `
        -Name "second_cash_usage_ratio" `
        -LegacyName "afternoon_cash_usage_ratio" `
        -Value $secondRatio

    Write-ConfigAtomically -Path $configPath -Config $candidate
    $candidateActive = $true
    Reset-RuntimeConfigCache

    # Validate with the same parser used by both production executors.
    $validatedFirstTime = Format-ReverseRepoClockTime `
        (Get-ReverseRepoFirstExecutionTime)
    $validatedSecondTime = Format-ReverseRepoClockTime `
        (Get-ReverseRepoSecondExecutionTime)
    $validatedFirstRatio = Get-ReverseRepoFirstCashUsageRatio
    $validatedSecondRatio = Get-ReverseRepoSecondCashUsageRatio

    Write-Output "Running full local verification..."
    $windowsPowerShell = Get-ReverseRepoPowerShell
    $verifyLogDirectory = Join-Path $repoRoot "logs"
    New-Item -ItemType Directory -Force -Path $verifyLogDirectory | Out-Null
    $verifyLogPath = Join-Path `
        $verifyLogDirectory `
        ("strategy_verify_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
    $verifyOutput = @(
        & $windowsPowerShell `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $verifyPath *>&1 |
            ForEach-Object { Out-String -InputObject $_ -Width 4096 }
    )
    $verifyExit = $LASTEXITCODE
    $verifyOutput | Set-Content -LiteralPath $verifyLogPath -Encoding UTF8
    if ($null -eq $verifyExit -or [int]$verifyExit -ne 0) {
        Write-Output "本地验证失败，完整日志：$verifyLogPath"
        Write-Output "--- 失败输出（末尾 30 行）---"
        $verifyOutput | Select-Object -Last 30
        throw "Verification failed for the candidate parameters."
    }
    Write-Output "本地验证通过，完整日志：$verifyLogPath"
    $stageLabels = [ordered]@{
        "compatibility passed" = "Windows PowerShell 5.1 兼容性"
        "install-root discovery" = "QMT 路径解析"
        "Boolean gate test passed" = "账户绑定门禁"
        "UTF-8 JSON test passed" = "UTF-8 JSON"
        "configuration tests passed" = "策略配置事务"
        "update and rollback tests passed" = "无Git更新/回滚"
        "isolation test passed" = "Python 隔离"
        "Runtime parameters valid" = "运行时参数"
        "Ran \d+ tests" = "单元测试"
        "Release package verification passed" = "发布包校验"
        "reverse_repo verification passed" = "整体验证"
    }
    foreach ($line in $verifyOutput) {
        $trimmed = $line.Trim()
        foreach ($marker in $stageLabels.Keys) {
            if ($trimmed -match $marker) {
                Write-Output "  ✓ $($stageLabels[$marker])"
                break
            }
        }
    }

    # Prevent a concurrent rr on from winning while verification was running.
    Assert-LiveStrategyIsOff
    Write-Output ""
    Write-Output "Verified candidate parameters:"
    $verifiedDisplay = [pscustomobject]@{
        first_execution_time = $validatedFirstTime
        first_cash_usage_ratio = $validatedFirstRatio
        second_execution_time = $validatedSecondTime
        second_cash_usage_ratio = $validatedSecondRatio
    }
    Write-Output (($verifiedDisplay | Format-List | Out-String).TrimEnd())
    if ($NonInteractiveConfirmed) {
        $confirmation = "Y"
    }
    else {
        $confirmationPrompt = @(
            "",
            "Save the verified parameters?",
            "  Y = save",
            "  N or Q = cancel and restore the previous configuration",
            "Choice"
        ) -join [Environment]::NewLine
        $confirmation = Read-Host $confirmationPrompt
    }
    if ($confirmation.Trim() -ine "Y") {
        Write-Output "Confirmation declined; restoring the previous configuration."
        return
    }

    Assert-LiveStrategyIsOff
    $committed = $true
    Write-Output "Verified parameters were saved. Live tasks remain Disabled."
    Write-Output "Review .\rr stat, then run .\rr on when ready."
}
catch [OperationCanceledException] {
    Write-Output $_.Exception.Message
}
finally {
    if ($candidateActive -and -not $committed) {
        Write-BytesAtomically -Path $configPath -Bytes $originalBytes
        Reset-RuntimeConfigCache
        Write-Output "Previous runtime configuration restored."
    }
}
