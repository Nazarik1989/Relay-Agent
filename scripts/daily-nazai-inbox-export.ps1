param(
  [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
  [switch]$SyncVps,
  [string]$Config = "config.json"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$env:PYTHONDONTWRITEBYTECODE = "1"

$logDir = Join-Path $root "automation-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd-HHmmss"
$logPath = Join-Path $logDir "$stamp-daily-nazai-inbox-export.log"

try {
  "[$(Get-Date -Format o)] Daily Naz inbox export started. Date=$Date SyncVps=$SyncVps" | Tee-Object -FilePath $logPath
  $argsList = @(
    "-m", "agent_content",
    "export-nazai-inbox",
    "--config", $Config,
    "--date", $Date
  )
  if ($SyncVps) {
    $argsList += "--sync-vps"
  }
  python @argsList 2>&1 | Tee-Object -FilePath $logPath -Append
  if ($LASTEXITCODE -ne 0) {
    throw "agent_content export failed with exit code $LASTEXITCODE"
  }
  "[$(Get-Date -Format o)] Daily Naz inbox export finished." | Tee-Object -FilePath $logPath -Append
} catch {
  "[$(Get-Date -Format o)] ERROR: $_" | Tee-Object -FilePath $logPath -Append
  throw
}
