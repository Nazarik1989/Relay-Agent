param(
    [string]$Config = "config.json"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m agent_content export-nazai-inbox-all --config $Config --sync-vps
if ($LASTEXITCODE -ne 0) {
    throw "NazAI history export failed with exit code $LASTEXITCODE"
}
