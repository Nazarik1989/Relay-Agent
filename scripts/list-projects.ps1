param(
    [string]$Config = "config.json"
)

python -m agent_content list-projects --config $Config
