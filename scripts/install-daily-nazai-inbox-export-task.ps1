param(
  [string]$TaskName = "AgentContentDailyNazInboxExport",
  [string]$Time = "23:55",
  [switch]$SyncVps
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $PSScriptRoot "daily-nazai-inbox-export.ps1"

if (-not (Test-Path -LiteralPath $scriptPath)) {
  throw "Daily export script not found: $scriptPath"
}

$quotedScript = '"' + $scriptPath + '"'
$taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $quotedScript"
if ($SyncVps) {
  $taskCommand += " -SyncVps"
}

schtasks.exe /Create /TN $TaskName /TR $taskCommand /SC DAILY /ST $Time /F | Out-Host

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Time: $Time"
Write-Host "Command: $taskCommand"
Write-Host "Workspace: $root"
