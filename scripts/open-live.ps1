$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

$livePath = Join-Path (Get-Location) "outputs\live-chronicle.md"
if (-not (Test-Path -LiteralPath $livePath)) {
  $env:PYTHONDONTWRITEBYTECODE = "1"
  python -m agent_content watch --once
}

if (Get-Command code -ErrorAction SilentlyContinue) {
  code $livePath
} else {
  Invoke-Item $livePath
}
