[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
$configDirectory = Join-Path $repoRoot "config"
$configPath = Join-Path `
    $configDirectory `
    "repo_failure_wxpusher.local.json"
$secretPath = Join-Path `
    $configDirectory `
    "repo_failure_wxpusher_secret.local.clixml"
$pythonPath = Get-ReverseRepoPython
$alertScript = Join-Path $PSScriptRoot "repo_failure_alert.py"

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

Write-Output "配置 miniQMT 微信推送通知（WxPusher 极简推送 SPT）。"
Write-Output ""
Write-Output "获取 SPT 只需一次，无需注册或创建应用："
Write-Output "  1. 手机微信扫描 WxPusher 官方 SPT 二维码（https://wxpusher.zjiecode.com/docs/spt.html）；"
Write-Output "  2. 按提示确认后，会得到一串以 SPT_ 开头的令牌，长按复制它；"
Write-Output "  3. 在手机微信里把它发给“文件传输助手”，再到电脑微信中复制；"
Write-Output "     或直接安装 WxPusher Windows 客户端，扫码登录后在客户端里复制；"
Write-Output "  4. 回到本窗口，在下面右键粘贴（或按 Ctrl+V），令牌会显示为 * 号，直接回车。"
Write-Output ""
Write-Output "令牌绑定你的微信，收到的通知只会发给你；无需手敲，粘贴即可。"
Write-Output ""
Write-Output "SPT 将使用 Windows 当前用户 DPAPI 加密，仅能由当前用户解密。"
Write-Output ""

$spt = ""
$confirmed = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $secureSpt = Read-Host "WxPusher SPT 令牌（右键粘贴后回车）" -AsSecureString
    $plainSpt = ConvertTo-PlainText -SecureValue $secureSpt
    if (
        [string]::IsNullOrWhiteSpace($plainSpt) `
        -or -not $plainSpt.Trim().StartsWith("SPT_") `
        -or $plainSpt.Trim().Length -gt 256 `
        -or $plainSpt.Contains("`r") `
        -or $plainSpt.Contains("`n")
    ) {
        Write-Warning "SPT 令牌无效：应为以 SPT_ 开头的一串字符。"
        continue
    }
    $spt = $plainSpt.Trim()
    $tail = if ($spt.Length -ge 6) {
        $spt.Substring($spt.Length - 6)
    }
    else {
        $spt
    }
    $answer = Read-Host "已收到令牌，末尾为 ***$tail，是否正确？[Y/n]"
    if (
        [string]::IsNullOrWhiteSpace($answer) `
        -or $answer.Trim().ToLowerInvariant() -eq "y"
    ) {
        $confirmed = $true
        break
    }
}
if (-not $confirmed -or [string]::IsNullOrWhiteSpace($spt)) {
    throw "未获得有效的 WxPusher SPT 令牌。"
}

New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
$config = [ordered]@{
    schema_version = 1
    enabled = $true
    transport = "wxpusher"
    timeout_seconds = 10
    attempts = 3
}
$configJson = $config | ConvertTo-Json -Depth 4
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $configPath,
    $configJson + [Environment]::NewLine,
    $utf8NoBom
)
$secureSpt | Export-Clixml -LiteralPath $secretPath -Force

$env:MINIQMT_ALERT_WXPUSHER_TOKEN = $spt
try {
    & $pythonPath $alertScript "--config" $configPath
    if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
        throw "WxPusher 配置校验失败。"
    }
    $answer = Read-Host "是否立即发送一条测试通知？[Y/n]"
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
            throw "测试通知发送失败。"
        }
    }
}
finally {
    Remove-Item Env:MINIQMT_ALERT_WXPUSHER_TOKEN `
        -ErrorAction SilentlyContinue
    $plainSpt = $null
    $spt = $null
}

Write-Output ""
Write-Output "微信推送通知配置完成："
Write-Output "  配置文件：$configPath"
Write-Output "  加密令牌：$secretPath"
Write-Output ""
Write-Output "提示：如同时配置了邮件通知，两种通道都会发送；"
Write-Output "只配置其中一种时，只走已配置的通道。"
