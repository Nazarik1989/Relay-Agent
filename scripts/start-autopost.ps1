param(
  [string]$Times = "",
  [int]$Interval = 30,
  [ValidateSet("pick", "daily")]
  [string]$Source = "pick"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONDONTWRITEBYTECODE = "1"

if ([string]::IsNullOrWhiteSpace($Times)) {
  python -m agent_content autopost --interval $Interval --source $Source
} else {
  python -m agent_content autopost --times $Times --interval $Interval --source $Source
}
