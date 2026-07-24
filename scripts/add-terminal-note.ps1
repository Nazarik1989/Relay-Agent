param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string]$Text
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m agent_content terminal-note $Text
