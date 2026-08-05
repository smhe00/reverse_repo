Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("reverse_repo_cfg_test_" + [guid]::NewGuid().ToString("N"))

function Write-Utf8Text {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $utf8)
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Set-PromptAnswers {
    param([Parameter(Mandatory = $true)][string[]]$Answers)
    $global:ReverseRepoCfgPromptAnswers = `
        New-Object System.Collections.Queue
    foreach ($answer in $Answers) {
        $global:ReverseRepoCfgPromptAnswers.Enqueue($answer)
    }
}

function global:Read-Host {
    [CmdletBinding()]
    param([string]$Prompt)
    if ($global:ReverseRepoCfgPromptAnswers.Count -eq 0) {
        throw "Unexpected prompt: $Prompt"
    }
    return [string]$global:ReverseRepoCfgPromptAnswers.Dequeue()
}

function global:Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName)
    return $null
}

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    New-Item `
        -ItemType Directory `
        -Path (Join-Path $testRoot "scripts") |
        Out-Null
    New-Item `
        -ItemType Directory `
        -Path (Join-Path $testRoot "config") |
        Out-Null
    Copy-Item `
        -LiteralPath (Join-Path $repoRoot "scripts\reverse_repo_runtime.ps1") `
        -Destination (Join-Path $testRoot "scripts")
    Copy-Item `
        -LiteralPath (
            Join-Path `
                $repoRoot `
                "scripts\configure_reverse_repo_strategy.ps1"
        ) `
        -Destination (Join-Path $testRoot "scripts")

    $configPath = Join-Path $testRoot "config\runtime.local.json"
    $exampleConfigPath = Join-Path `
        $testRoot `
        "config\runtime.example.json"
    $initialJson = @"
{
  "python_path": ".venv\\Scripts\\python.exe",
  "live_qmt_path": "D:\\LiveQMT",
  "first_execution_time": "09:30:42",
  "second_execution_time": "15:10:00",
  "first_cash_usage_ratio": 0.9,
  "second_cash_usage_ratio": 1.0
}
"@
    Write-Utf8Text -Path $configPath -Text $initialJson
    $exampleJson = @"
{
  "python_path": ".venv\\Scripts\\python.exe",
  "live_qmt_path": "D:\\LiveQMT",
  "first_execution_time": "09:30:42",
  "second_execution_time": "15:10:00",
  "first_cash_usage_ratio": 0.9,
  "second_cash_usage_ratio": 1.0
}
"@
    Write-Utf8Text -Path $exampleConfigPath -Text $exampleJson
    $originalBytes = [System.IO.File]::ReadAllBytes($configPath)
    $configurator = Join-Path `
        $testRoot `
        "scripts\configure_reverse_repo_strategy.ps1"
    $verifyPath = Join-Path $testRoot "verify.ps1"

    Write-Utf8Text -Path $verifyPath -Text "exit 0`r`n"
    Set-PromptAnswers -Answers @(
        "10:00:00", "0.5", "15:20:00", "0.8", "N"
    )
    & $configurator | Out-Null
    $afterDecline = [System.IO.File]::ReadAllBytes($configPath)
    Assert-True `
        -Condition ([Convert]::ToBase64String($afterDecline) -eq `
            [Convert]::ToBase64String($originalBytes)) `
        -Message "Declined candidate did not restore the exact config bytes."

    Set-PromptAnswers -Answers @("Q")
    & $configurator | Out-Null
    $afterCancel = [System.IO.File]::ReadAllBytes($configPath)
    Assert-True `
        -Condition ([Convert]::ToBase64String($afterCancel) -eq `
            [Convert]::ToBase64String($originalBytes)) `
        -Message "A quiet cancellation changed the runtime configuration."

    # Parameter saving no longer runs the full local verification; a broken
    # verify.ps1 must not affect the configurator.
    Write-Utf8Text -Path $verifyPath -Text "throw 'verify must not run'`r`n"
    Set-PromptAnswers -Answers @(
        "10:00:00", "0.5", "15:20:00", "0.8", "Y"
    )
    & $configurator | Out-Null
    $savedWithoutVerify = `
        Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    Assert-True `
        -Condition ($savedWithoutVerify.first_execution_time -eq "10:00:00") `
        -Message "Parameter saving unexpectedly depends on verify.ps1."

    Write-Utf8Text -Path $verifyPath -Text "exit 0`r`n"
    Set-PromptAnswers -Answers @(
        "09:29:00", "10:00:00",
        "1.1", "0.5",
        "16:0:0", "15:20:00",
        "0.8", "Y"
    )
    & $configurator | Out-Null
    $saved = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    Assert-True `
        -Condition ($saved.first_execution_time -eq "10:00:00") `
        -Message "Confirmed first execution time was not saved."
    Assert-True `
        -Condition ([double]$saved.first_cash_usage_ratio -eq 0.5) `
        -Message "Confirmed first ratio was not saved."
    Assert-True `
        -Condition ($saved.second_execution_time -eq "15:20:00") `
        -Message "Confirmed second execution time was not saved."
    Assert-True `
        -Condition ([double]$saved.second_cash_usage_ratio -eq 0.8) `
        -Message "Confirmed second ratio was not saved."
    Assert-True `
        -Condition ($global:ReverseRepoCfgPromptAnswers.Count -eq 0) `
        -Message "The expected final confirmation prompt did not run."

    Set-PromptAnswers -Answers @("D", "D", "D", "D", "Y")
    & $configurator | Out-Null
    $restoredDefaults = `
        Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    Assert-True `
        -Condition ($restoredDefaults.first_execution_time -eq "09:30:42") `
        -Message "D did not restore the sample first execution time."
    Assert-True `
        -Condition ([double]$restoredDefaults.first_cash_usage_ratio -eq 0.9) `
        -Message "D did not restore the sample first ratio."
    Assert-True `
        -Condition ($restoredDefaults.second_execution_time -eq "15:10:00") `
        -Message "D did not restore the sample second execution time."
    Assert-True `
        -Condition ([double]$restoredDefaults.second_cash_usage_ratio -eq 1.0) `
        -Message "D did not restore the sample second ratio."

    & $configurator `
        -FirstExecutionTime "10:05:00" `
        -FirstCashUsageRatio "0.6" `
        -SecondExecutionTime "15:15:00" `
        -SecondCashUsageRatio "0.7" `
        -NonInteractiveConfirmed |
        Out-Null
    $nonInteractive = `
        Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    Assert-True `
        -Condition ($nonInteractive.first_execution_time -eq "10:05:00") `
        -Message "Confirmed non-interactive first time was not saved."
    Assert-True `
        -Condition ([double]$nonInteractive.first_cash_usage_ratio -eq 0.6) `
        -Message "Confirmed non-interactive first ratio was not saved."
    Assert-True `
        -Condition ($nonInteractive.second_execution_time -eq "15:15:00") `
        -Message "Confirmed non-interactive second time was not saved."
    Assert-True `
        -Condition ([double]$nonInteractive.second_cash_usage_ratio -eq 0.7) `
        -Message "Confirmed non-interactive second ratio was not saved."

    $beforeUnconfirmed = [System.IO.File]::ReadAllBytes($configPath)
    $unconfirmedRejected = $false
    try {
        & $configurator `
            -FirstExecutionTime "10:10:00" `
            -FirstCashUsageRatio "0.4" `
            -SecondExecutionTime "15:20:00" `
            -SecondCashUsageRatio "0.6" |
            Out-Null
    }
    catch {
        $unconfirmedRejected = $true
    }
    Assert-True `
        -Condition $unconfirmedRejected `
        -Message "Command-line values were accepted without explicit confirmation."
    $afterUnconfirmed = [System.IO.File]::ReadAllBytes($configPath)
    Assert-True `
        -Condition ([Convert]::ToBase64String($afterUnconfirmed) -eq `
            [Convert]::ToBase64String($beforeUnconfirmed)) `
        -Message "Rejected command-line values changed the config bytes."

    Write-Output "Transactional strategy configuration tests passed."
}
finally {
    Remove-Item `
        -LiteralPath Function:\Read-Host `
        -ErrorAction SilentlyContinue
    Remove-Item `
        -LiteralPath Function:\Get-ScheduledTask `
        -ErrorAction SilentlyContinue
    Remove-Variable `
        -Name ReverseRepoCfgPromptAnswers `
        -Scope Global `
        -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
