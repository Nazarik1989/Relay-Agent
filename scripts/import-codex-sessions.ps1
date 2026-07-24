param(
    [string]$Config = "config.json",
    [ValidateSet("brief", "detailed", "both")]
    [string]$Format = "detailed",
    [ValidateSet("central", "project")]
    [string]$Layout = "central",
    [switch]$Clear
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONDONTWRITEBYTECODE = "1"

$argsList = @(
    "-m", "agent_content",
    "import-codex-sessions",
    "--config", $Config,
    "--format", $Format,
    "--layout", $Layout
)

if ($Clear) {
    $argsList += "--clear"
}

python @argsList
