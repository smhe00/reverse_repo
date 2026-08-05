Set-StrictMode -Version Latest

$script:ReverseRepoRoot = Split-Path -Parent $PSScriptRoot
$script:ReverseRepoRuntimeConfig = $null

function Get-ReverseRepoRoot {
    return $script:ReverseRepoRoot
}

function Get-ReverseRepoPowerShell {
    $path = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Windows PowerShell 5.1 is required: $path"
    }
    return $path
}

function Read-ReverseRepoJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "JSON file is missing: $Path"
    }
    try {
        # Windows PowerShell 5.1 treats BOM-less UTF-8 as the active ANSI
        # code page. All project JSON is UTF-8, including Chinese QMT paths.
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $json = [System.IO.File]::ReadAllText($Path, $strictUtf8)
        return (ConvertFrom-Json -InputObject $json -ErrorAction Stop)
    }
    catch {
        throw (
            "Invalid UTF-8 JSON file: $Path. " +
            $_.Exception.Message
        )
    }
}

function Get-ReverseRepoSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Hash input file is missing: $Path"
    }
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha256.ComputeHash($stream)
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
    return ([System.BitConverter]::ToString($digest)).Replace(
        "-",
        ""
    ).ToLowerInvariant()
}

function Get-ReverseRepoRuntimeConfig {
    if ($null -ne $script:ReverseRepoRuntimeConfig) {
        return $script:ReverseRepoRuntimeConfig
    }
    $path = Join-Path `
        $script:ReverseRepoRoot `
        "config\runtime.local.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw (
            "Runtime configuration is missing: $path. " +
            "Copy config\runtime.example.json to config\runtime.local.json " +
            "and edit the machine-specific paths."
        )
    }
    $script:ReverseRepoRuntimeConfig = Read-ReverseRepoJson -Path $path
    return $script:ReverseRepoRuntimeConfig
}

function Resolve-ReverseRepoConfiguredPath {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Configured path cannot be empty."
    }
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath(
        (Join-Path $script:ReverseRepoRoot $Value)
    )
}

function Get-ReverseRepoPython {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace(
        $env:REVERSE_REPO_PYTHON
    )) {
        $candidates.Add($env:REVERSE_REPO_PYTHON)
    }
    $config = Get-ReverseRepoRuntimeConfig
    if (-not [string]::IsNullOrWhiteSpace(
        [string]$config.python_path
    )) {
        $candidates.Add(
            (Resolve-ReverseRepoConfiguredPath `
                -Value ([string]$config.python_path))
        )
    }
    $candidates.Add(
        (Join-Path $script:ReverseRepoRoot ".venv\Scripts\python.exe")
    )
    $workspaceRoot = Split-Path -Parent $script:ReverseRepoRoot
    $candidates.Add(
        (Join-Path $workspaceRoot ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $command = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    throw "No usable Python executable was found for reverse_repo."
}

function Get-ReverseRepoLiveQmtPath {
    $override = $env:REVERSE_REPO_LIVE_QMT_PATH
    if (-not [string]::IsNullOrWhiteSpace($override)) {
        return Resolve-ReverseRepoConfiguredPath -Value $override
    }
    $config = Get-ReverseRepoRuntimeConfig
    $value = [string]$config.live_qmt_path
    $path = Resolve-ReverseRepoConfiguredPath -Value $value
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "Configured live QMT path does not exist: $path"
    }
    return $path
}

function Get-ReverseRepoSimulationQmtPath {
    $override = $env:REVERSE_REPO_SIMULATION_QMT_PATH
    if (-not [string]::IsNullOrWhiteSpace($override)) {
        return Resolve-ReverseRepoConfiguredPath -Value $override
    }
    $config = Get-ReverseRepoRuntimeConfig
    $value = [string]$config.simulation_qmt_path
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw (
            "Developer simulation validation requires simulation_qmt_path " +
            "in config\runtime.local.json. Run .\rr dev bind first."
        )
    }
    $path = Resolve-ReverseRepoConfiguredPath -Value $value
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "Configured simulation QMT path does not exist: $path"
    }
    return $path
}

function Get-ReverseRepoConfigValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][object]$Default
    )
    $config = Get-ReverseRepoRuntimeConfig
    $property = $config.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return $Default
    }
    return $property.Value
}

function Get-ReverseRepoAliasedConfigValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$LegacyName,
        [Parameter(Mandatory = $true)][object]$Default
    )
    $config = Get-ReverseRepoRuntimeConfig
    $current = $config.PSObject.Properties[$Name]
    $legacy = $config.PSObject.Properties[$LegacyName]
    if ($null -ne $current -and $null -ne $legacy) {
        if ([string]$current.Value -ne [string]$legacy.Value) {
            throw (
                "$Name and legacy $LegacyName conflict; keep only $Name."
            )
        }
        return $current.Value
    }
    if ($null -ne $current -and $null -ne $current.Value) {
        return $current.Value
    }
    if ($null -ne $legacy -and $null -ne $legacy.Value) {
        return $legacy.Value
    }
    return $Default
}

function ConvertTo-ReverseRepoClockTime {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$Name
    )
    try {
        $parsed = [TimeSpan]::ParseExact(
            [string]$Value,
            "hh\:mm\:ss",
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch [FormatException] {
        throw "$Name must use HH:mm:ss format."
    }
    if ($parsed.TotalSeconds -lt 0 -or $parsed.TotalHours -ge 24) {
        throw "$Name must be a time within one calendar day."
    }
    return $parsed
}

function Get-ReverseRepoFirstExecutionTime {
    $value = Get-ReverseRepoAliasedConfigValue `
        -Name "first_execution_time" `
        -LegacyName "morning_execution_time" `
        -Default "09:30:42"
    $parsed = ConvertTo-ReverseRepoClockTime `
        -Value $value `
        -Name "first_execution_time"
    $morningStart = [TimeSpan]::FromHours(9.5)
    $morningLatest = [TimeSpan]::FromHours(11) + `
        [TimeSpan]::FromMinutes(28)
    $afternoonStart = [TimeSpan]::FromHours(13)
    $afternoonLatest = [TimeSpan]::FromHours(15) + `
        [TimeSpan]::FromMinutes(28)
    $valid = (
        ($parsed -ge $morningStart -and $parsed -le $morningLatest) `
        -or (
            $parsed -ge $afternoonStart `
            -and $parsed -le $afternoonLatest
        )
    )
    if (-not $valid) {
        throw (
            "first_execution_time must be from 09:30:00 through " +
            "11:28:00 or from 13:00:00 through 15:28:00."
        )
    }
    return $parsed
}

function Get-ReverseRepoSecondExecutionTime {
    $value = Get-ReverseRepoAliasedConfigValue `
        -Name "second_execution_time" `
        -LegacyName "afternoon_execution_time" `
        -Default "15:10:00"
    $parsed = ConvertTo-ReverseRepoClockTime `
        -Value $value `
        -Name "second_execution_time"
    $morningStart = [TimeSpan]::FromHours(9.5)
    $morningEnd = [TimeSpan]::FromHours(11.5)
    $afternoonStart = [TimeSpan]::FromHours(13)
    $afternoonEnd = [TimeSpan]::FromHours(15.5)
    $valid = (
        ($parsed -ge $morningStart -and $parsed -lt $morningEnd) `
        -or (
            $parsed -ge $afternoonStart `
            -and $parsed -lt $afternoonEnd
        )
    )
    if (-not $valid) {
        throw (
            "second_execution_time must be from 09:30:00 before " +
            "11:30:00 or from 13:00:00 before 15:30:00."
        )
    }
    $first = Get-ReverseRepoFirstExecutionTime
    if (($parsed - $first) -lt [TimeSpan]::FromMinutes(5)) {
        throw (
            "second_execution_time must be at least five minutes " +
            "after first_execution_time."
        )
    }
    return $parsed
}

function Get-ReverseRepoFirstCashUsageRatio {
    $value = Get-ReverseRepoAliasedConfigValue `
        -Name "first_cash_usage_ratio" `
        -LegacyName "morning_cash_usage_ratio" `
        -Default 0.90
    try {
        $parsed = [Convert]::ToDouble(
            $value,
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch [FormatException] {
        throw "first_cash_usage_ratio must be a number."
    }
    if (
        [double]::IsNaN($parsed) `
        -or [double]::IsInfinity($parsed) `
        -or $parsed -lt 0 `
        -or $parsed -gt 1
    ) {
        throw "first_cash_usage_ratio must be from 0 through 1."
    }
    return $parsed
}

function Get-ReverseRepoSecondCashUsageRatio {
    $value = Get-ReverseRepoAliasedConfigValue `
        -Name "second_cash_usage_ratio" `
        -LegacyName "afternoon_cash_usage_ratio" `
        -Default 1.0
    try {
        $parsed = [Convert]::ToDouble(
            $value,
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    catch [FormatException] {
        throw "second_cash_usage_ratio must be a number."
    }
    if (
        [double]::IsNaN($parsed) `
        -or [double]::IsInfinity($parsed) `
        -or $parsed -lt 0 `
        -or $parsed -gt 1
    ) {
        throw "second_cash_usage_ratio must be from 0 through 1."
    }
    return $parsed
}

# Compatibility aliases for existing wrappers and local tooling.
function Get-ReverseRepoMorningExecutionTime {
    return Get-ReverseRepoFirstExecutionTime
}

function Get-ReverseRepoAfternoonExecutionTime {
    return Get-ReverseRepoSecondExecutionTime
}

function Get-ReverseRepoMorningCashUsageRatio {
    return Get-ReverseRepoFirstCashUsageRatio
}

function Get-ReverseRepoAfternoonCashUsageRatio {
    return Get-ReverseRepoSecondCashUsageRatio
}

function Format-ReverseRepoClockTime {
    param([Parameter(Mandatory = $true)][TimeSpan]$Value)
    return ([datetime]::Today.Add($Value).ToString("HH:mm:ss"))
}

function Get-ReverseRepoTaskStartTime {
    param(
        [Parameter(Mandatory = $true)][TimeSpan]$ExecutionTime,
        [Parameter(Mandatory = $true)][int]$LeadSeconds
    )
    $start = $ExecutionTime - [TimeSpan]::FromSeconds($LeadSeconds)
    if ($start.TotalSeconds -lt 0) {
        throw "Task start time would fall on the previous calendar day."
    }
    return $start
}

function Get-ReverseRepoLiveEnableManifestPath {
    return (Join-Path `
        $script:ReverseRepoRoot `
        "config\repo_live_enable_manifest.local.json")
}

function Invoke-ReverseRepoLiveEnableManifest {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("create", "verify")]
        [string]$Mode
    )
    $pythonPath = Get-ReverseRepoPython
    $scriptPath = Join-Path `
        $PSScriptRoot `
        "repo_live_enable_manifest.py"
    $strategyConfigPath = Join-Path `
        $script:ReverseRepoRoot `
        "config\runtime.local.json"
    $certificatePath = Join-Path `
        $script:ReverseRepoRoot `
        "reports\gc001_intraday\live_channel_validation\latest.json"
    $signingKeyPath = Join-Path `
        $script:ReverseRepoRoot `
        "config\repo_release_gate_secret.local.json"
    $manifestPath = Get-ReverseRepoLiveEnableManifestPath
    foreach ($requiredPath in @(
        $pythonPath,
        $scriptPath,
        $strategyConfigPath,
        $certificatePath,
        $signingKeyPath
    )) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Live-enable snapshot dependency is missing: $requiredPath"
        }
    }
    & $pythonPath `
        $scriptPath `
        $Mode `
        "--strategy-config" `
        $strategyConfigPath `
        "--live-channel-certificate" `
        $certificatePath `
        "--signing-key" `
        $signingKeyPath `
        "--manifest" `
        $manifestPath
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "Live-enable snapshot $Mode failed."
    }
}

function New-ReverseRepoLiveEnableManifest {
    Invoke-ReverseRepoLiveEnableManifest -Mode "create"
}

function Assert-ReverseRepoLiveEnableManifest {
    Invoke-ReverseRepoLiveEnableManifest -Mode "verify"
}

function Remove-ReverseRepoLiveEnableManifest {
    $manifestPath = Get-ReverseRepoLiveEnableManifestPath
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        Remove-Item -LiteralPath $manifestPath -Force
    }
}

function Enable-ReverseRepoOptionalFailureEmail {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$SecretPath
    )
    Remove-Item Env:MINIQMT_ALERT_SMTP_PASSWORD `
        -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        Write-Warning (
            "Optional failure email is disabled because the configuration " +
            "file does not exist: $ConfigPath"
        )
        return $false
    }
    if (-not (Test-Path -LiteralPath $SecretPath -PathType Leaf)) {
        Write-Warning (
            "Optional failure email is disabled because the secret file " +
            "does not exist: $SecretPath"
        )
        return $false
    }
    try {
        $securePassword = Import-Clixml -LiteralPath $SecretPath
        if ($securePassword -isnot [securestring]) {
            throw "Failure-email secret is not a Windows SecureString."
        }
        $credential = [pscredential]::new("smtp", $securePassword)
        $env:MINIQMT_ALERT_SMTP_PASSWORD = (
            $credential.GetNetworkCredential().Password
        )
        return $true
    }
    catch {
        Remove-Item Env:MINIQMT_ALERT_SMTP_PASSWORD `
            -ErrorAction SilentlyContinue
        Write-Warning (
            "Optional failure email is disabled: " +
            $_.Exception.Message
        )
        return $false
    }
}

function Disable-ReverseRepoOptionalFailureEmail {
    Remove-Item Env:MINIQMT_ALERT_SMTP_PASSWORD `
        -ErrorAction SilentlyContinue
}
