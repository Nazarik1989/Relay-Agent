param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [string]$Name = "",
    [string]$Config = "config.json"
)

$argsList = @("add-project", $Path, "--config", $Config)
if ($Name.Trim()) {
    $argsList += @("--name", $Name)
}

python -m agent_content @argsList
