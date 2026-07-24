param(
  [switch]$SyncVps
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONDONTWRITEBYTECODE = "1"

if ($SyncVps) {
  python -m agent_content export-nazai-inbox-all --sync-vps
} else {
  python -m agent_content export-nazai-inbox-all
}
