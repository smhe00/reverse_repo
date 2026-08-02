Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "reverse_repo_runtime.ps1")
$repoRoot = Get-ReverseRepoRoot
Set-Location -LiteralPath $repoRoot

$pythonPath = Get-ReverseRepoPython
$qmtPath = Get-ReverseRepoQmtPath -Environment "simulation"
$bindingPath = Join-Path `
    $repoRoot `
    "config\repo_simulation_account_binding.local.json"
$signingKeyPath = Join-Path `
    $repoRoot `
    "config\repo_release_gate_secret.local.json"
$strategyConfigPath = Join-Path `
    $repoRoot `
    "config\runtime.local.json"
$dateStamp = Get-Date -Format "yyyyMMdd"
$validationDirectory = Join-Path `
    $repoRoot `
    "reports\gc001_intraday\simulation_validation"
$morningJournal = Join-Path `
    $validationDirectory `
    "morning_recovery_$dateStamp.journal.json"
$afternoonJournal = Join-Path `
    $validationDirectory `
    "afternoon_$dateStamp.journal.json"
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
    "--morning-journal" `
    $morningJournal `
    "--afternoon-journal" `
    $afternoonJournal `
    "--signing-key" `
    $signingKeyPath `
    "--strategy-config" `
    $strategyConfigPath `
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
