[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Developer-only isolation helpers. This file is intentionally NOT part of the
# protected execution source list (EXECUTION_SOURCE_FILES), so changing rr dev
# functionality here never invalidates the live certificate or affects the
# live .\rr on path.

function Assert-DeveloperSimulationIsolation {
    # Shared hard gate for developer simulation deployments. Resolves the
    # simulation QMT path and refuses anything that is not a simulation
    # client, and requires exactly one bound simulation security account.
    # Dev tasks are unrelated to the live strategy, so this gate deliberately
    # does NOT inspect live scheduled tasks or the live-enable manifest.
    $repoRoot = Get-ReverseRepoRoot
    $qmtPath = Get-ReverseRepoSimulationQmtPath
    if ([string]$qmtPath -notlike "*模拟*") {
        throw (
            "Developer tasks require a simulation QMT path " +
            "(must contain 模拟): $qmtPath"
        )
    }
    $bindingPath = Join-Path `
        $repoRoot `
        "config\repo_simulation_account_binding.local.json"
    if (-not (Test-Path -LiteralPath $bindingPath -PathType Leaf)) {
        throw "Simulation account binding is missing: $bindingPath"
    }
    $binding = Read-ReverseRepoJson -Path $bindingPath
    $simulationEntries = @(
        $binding.accounts |
            Where-Object {
                $_.environment -eq "simulation" `
                -and $_.account_type -eq "SECURITY_ACCOUNT"
            }
    )
    if ($simulationEntries.Count -ne 1) {
        throw "Exactly one simulation security-account binding is required."
    }
    if ($null -ne $simulationEntries[0].PSObject.Properties["account_id"]) {
        throw "Plaintext account IDs are forbidden in simulation bindings."
    }
}
