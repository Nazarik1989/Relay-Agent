param(
    [string]$Root = "C:\Projects",
    [switch]$Write,
    [string]$Config = "config.json"
)

$argsList = @("discover-projects", "--root", $Root, "--config", $Config)
if ($Write) {
    $argsList += "--write"
}

python -m agent_content @argsList
