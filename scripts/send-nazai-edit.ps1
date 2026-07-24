param(
  [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
  [ValidateSet("pick", "daily")]
  [string]$Source = "pick"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m agent_content nazai-edit --date $Date --source $Source --send
