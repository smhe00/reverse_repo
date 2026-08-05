param(
    [Parameter(Mandatory = $true)][datetime]$ValidationDate,
    [Parameter(Mandatory = $true)][string]$ValidationFirstExecutionTime,
    [Parameter(Mandatory = $true)][string]$ValidationSecondExecutionTime
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoSimulationQmtPath
$bindingPath = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
$signingKeyPath = Join-Path `
    $repoRoot `
    "config\repo_release_gate_secret.local.json"
$strategyConfigPath = Join-Path `
    $repoRoot `
    "config\runtime.local.json"
$dateStamp = $ValidationDate.ToString("yyyyMMdd")
$validationDirectory = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\simulation_validation"
$morningNormalJournal = Join-Path `
    $validationDirectory `
    "morning_normal_$dateStamp.journal.json"
$afternoonNormalJournal = Join-Path `
    $validationDirectory `
    "afternoon_$dateStamp.journal.json"
$morningRecoveryJournal = Join-Path `
    $validationDirectory `
    "morning_recovery_$dateStamp.journal.json"
$certificatePath = Join-Path $validationDirectory "latest.json"
$datedCertificatePath = Join-Path `
    $validationDirectory `
    "certificate_$dateStamp.json"
$logPath = Join-Path `
    $validationDirectory `
    "certificate_$dateStamp.log"

& $pythonPath `
    (Join-Path $PSScriptRoot "repo_simulation_validation.py") `
    "certify" `
    "--qmt-path" `
    $qmtPath `
    "--account-binding" `
    $bindingPath `
    "--morning-normal-journal" `
    $morningNormalJournal `
    "--afternoon-normal-journal" `
    $afternoonNormalJournal `
    "--morning-recovery-journal" `
    $morningRecoveryJournal `
    "--signing-key" `
    $signingKeyPath `
    "--strategy-config" `
    $strategyConfigPath `
    "--validation-first-execution-time" `
    $ValidationFirstExecutionTime `
    "--validation-second-execution-time" `
    $ValidationSecondExecutionTime `
    "--output" `
    $datedCertificatePath *>> $logPath
$result = $LASTEXITCODE
if ($null -eq $result -or [int]$result -ne 0) {
    throw "Simulation certification failed."
}
Copy-Item `
    -LiteralPath $datedCertificatePath `
    -Destination $certificatePath `
    -Force
exit 0
