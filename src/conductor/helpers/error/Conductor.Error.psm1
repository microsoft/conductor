# Conductor.Error.psm1 — PowerShell helper for raising typed Conductor
# error envelopes from script-type workflow nodes.
#
# Contract: write a single JSON object to $env:CONDUCTOR_ERROR_OUT and
# exit 0. Conductor reads the file, treats the node as raised, and
# evaluates on_error routes against the envelope.
#
# Usage:
#   Import-Module ./Conductor.Error.psm1
#   Write-ConductorError -Kind "external.git.fetch_failed" `
#                        -Message "remote rejected push" `
#                        -Details @{ remote = "origin"; exit = 128 }
#   exit 0

function Write-ConductorError {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][string]$Message,
        [Parameter(Mandatory = $false)][hashtable]$Details
    )

    if (-not $env:CONDUCTOR_ERROR_OUT) {
        throw "CONDUCTOR_ERROR_OUT is not set; this script must be run by Conductor as a script-type node."
    }

    $envelope = [ordered]@{
        conductor_error = $true
        kind            = $Kind
        message         = $Message
    }
    if ($PSBoundParameters.ContainsKey('Details') -and $null -ne $Details) {
        $envelope['details'] = $Details
    }

    $json = $envelope | ConvertTo-Json -Depth 16 -Compress
    Set-Content -Path $env:CONDUCTOR_ERROR_OUT -Value $json -Encoding utf8 -NoNewline
}

Export-ModuleMember -Function Write-ConductorError
