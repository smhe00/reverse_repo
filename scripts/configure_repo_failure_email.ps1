[CmdletBinding()]
param(
    [string]$Recipient = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$configDirectory = Join-Path $repoRoot "config"
$configPath = Join-Path `
    $configDirectory `
    "repo_failure_email.local.json"
$secretPath = Join-Path `
    $configDirectory `
    "repo_failure_email_secret.local.clixml"
$pythonPath = Get-ReverseRepoPython
$alertScript = Join-Path $PSScriptRoot "repo_failure_alert.py"

function Read-DefaultedValue {
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$Default
    )
    $value = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value.Trim()
}

function ConvertTo-PlainText {
    param(
        [Parameter(Mandatory = $true)]
        [securestring]$SecureValue
    )
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $SecureValue
    )
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python executable does not exist: $pythonPath"
}
if (-not (Test-Path -LiteralPath $alertScript -PathType Leaf)) {
    throw "Failure-alert script does not exist: $alertScript"
}

Write-Output "配置 miniQMT 逆回购失败邮件告警。"
Write-Output "密码将使用 Windows 当前用户 DPAPI 加密，仅能由当前用户解密。"
Write-Output ""

$toAddress = if ([string]::IsNullOrWhiteSpace($Recipient)) {
    (Read-Host "收件邮箱").Trim()
}
else {
    Read-DefaultedValue `
        -Prompt "收件邮箱" `
        -Default $Recipient
}
$fromAddress = Read-DefaultedValue `
    -Prompt "发件邮箱" `
    -Default $toAddress
$smtpHost = Read-DefaultedValue `
    -Prompt "SMTP 主机" `
    -Default "smtp-mail.outlook.com"
$defaultPort = if ($smtpHost -in @("smtp.163.com", "smtp.126.com")) {
    "465"
}
else {
    "587"
}
$defaultSecurity = if ($defaultPort -eq "465") {
    "ssl"
}
else {
    "starttls"
}
$smtpPortText = Read-DefaultedValue `
    -Prompt "SMTP 端口" `
    -Default $defaultPort
$smtpUsername = Read-DefaultedValue `
    -Prompt "SMTP 用户名" `
    -Default $fromAddress
$smtpSecurity = Read-DefaultedValue `
    -Prompt "加密方式 starttls/ssl" `
    -Default $defaultSecurity

$smtpPort = 0
if (
    -not [int]::TryParse($smtpPortText, [ref]$smtpPort) `
    -or $smtpPort -lt 1 `
    -or $smtpPort -gt 65535
) {
    throw "SMTP 端口无效。"
}
if ($smtpSecurity -notin @("starttls", "ssl")) {
    throw "加密方式只能是 starttls 或 ssl。"
}
if (
    $toAddress -notmatch "^[^@\s]+@[^@\s]+$" `
    -or $fromAddress -notmatch "^[^@\s]+@[^@\s]+$"
) {
    throw "邮箱地址格式无效。"
}

$securePassword = Read-Host "SMTP 密码或应用专用密码" -AsSecureString
$plainPassword = ConvertTo-PlainText -SecureValue $securePassword
if ([string]::IsNullOrEmpty($plainPassword)) {
    throw "SMTP 密码不能为空。"
}

New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
$config = [ordered]@{
    schema_version = 1
    enabled = $true
    transport = "smtp"
    to = @($toAddress)
    from = $fromAddress
    smtp_host = $smtpHost
    smtp_port = $smtpPort
    smtp_security = $smtpSecurity
    smtp_username = $smtpUsername
    timeout_seconds = 10
    attempts = 3
}
$config |
    ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $configPath -Encoding utf8NoBOM
$securePassword | Export-Clixml -LiteralPath $secretPath -Force

$env:MINIQMT_ALERT_SMTP_PASSWORD = $plainPassword
try {
    & $pythonPath $alertScript "--config" $configPath
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "邮件配置校验失败。"
    }
    $answer = Read-Host "是否立即发送一封测试邮件？[Y/n]"
    if (
        [string]::IsNullOrWhiteSpace($answer) `
        -or $answer.Trim().ToLowerInvariant() -eq "y"
    ) {
        & $pythonPath `
            $alertScript `
            "--config" `
            $configPath `
            "--test-send"
        if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
            throw "测试邮件发送失败。"
        }
    }
}
finally {
    Remove-Item Env:MINIQMT_ALERT_SMTP_PASSWORD `
        -ErrorAction SilentlyContinue
    $plainPassword = $null
}

Write-Output ""
Write-Output "失败邮件告警配置完成："
Write-Output "  收件人：$toAddress"
Write-Output "  配置文件：$configPath"
Write-Output "  加密密码：$secretPath"
