param(
  [int]$Interval = 60,
  [ValidateSet("live", "daily", "full")]
  [string]$SendKind = "full"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m agent_content watch --interval $Interval --send-on-stop --send-kind $SendKind
