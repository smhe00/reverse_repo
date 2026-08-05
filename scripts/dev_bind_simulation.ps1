[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")

$runtimeConfigPath = Join-Path $repoRoot "config\runtime.local.json"
if (-not (Test-Path -LiteralPath $runtimeConfigPath -PathType Leaf)) {
    throw "Runtime configuration is missing. Run .\rr init first."
}
$config = Read-ReverseRepoJson -Path $runtimeConfigPath
$existing = [string]$config.simulation_qmt_path
$suggestedRoot = if ([string]::IsNullOrWhiteSpace($existing)) {
    "D:\国金QMT交易端模拟"
}
else {
    $existing
}

$inputPath = Read-Host "模拟miniQMT安装目录 [$suggestedRoot]"
if ([string]::IsNullOrWhiteSpace($inputPath)) {
    $inputPath = $suggestedRoot
}
$resolved = [System.IO.Path]::GetFullPath(
    $inputPath.Trim().Trim('"')
)
$installRoot = if ((Split-Path -Leaf $resolved) -eq "userdata_mini") {
    Split-Path -Parent $resolved
}
else {
    $resolved
}
if ($installRoot -notmatch "模拟") {
    throw (
        "开发者模拟验证要求模拟miniQMT安装目录包含【模拟】：" +
        $installRoot
    )
}
if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
    throw "模拟miniQMT安装目录不存在：$installRoot"
}
$userdataPath = Join-Path $installRoot "userdata_mini"
if (-not (Test-Path -LiteralPath $userdataPath -PathType Container)) {
    throw (
        "尚未检测到 $userdataPath。请先在模拟miniQMT中勾选" +
        "【独立交易】并登录一次，再运行 .\rr dev bind。"
    )
}

# Preserve every existing runtime field and add the developer-only
# simulation path, written atomically as UTF-8 without BOM.
$payload = [ordered]@{}
foreach ($property in $config.PSObject.Properties) {
    $payload[$property.Name] = $property.Value
}
$payload["simulation_qmt_path"] = $userdataPath
$utf8 = New-Object System.Text.UTF8Encoding($false)
$json = ($payload | ConvertTo-Json -Depth 8) + [Environment]::NewLine
$temporaryPath = Join-Path `
    (Split-Path -Parent $runtimeConfigPath) `
    (".runtime.local.tmp." + [guid]::NewGuid().ToString("N"))
try {
    [System.IO.File]::WriteAllText($temporaryPath, $json, $utf8)
    Move-Item -LiteralPath $temporaryPath -Destination $runtimeConfigPath -Force
}
finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}
$script:ReverseRepoRuntimeConfig = $null

$pythonPath = Get-ReverseRepoPython
$bindingOutput = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
& $pythonPath `
    (Join-Path $PSScriptRoot "bootstrap_repo_account_binding.py") `
    "--qmt-path" `
    $userdataPath `
    "--environment" `
    "simulation" `
    "--label" `
    "repo_simulation" `
    "--output" `
    $bindingOutput
if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -ne 0) {
    throw "Simulation account binding failed."
}

Write-Output "开发者模拟环境就绪：$userdataPath"
Write-Output "模拟账户绑定：$bindingOutput"
Write-Output "下一步：.\rr dev cert [日期] 部署模拟认证，或 .\rr dev stress [日期] 部署压力测试。"
