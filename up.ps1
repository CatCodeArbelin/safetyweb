$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $RepoRoot "scripts/dev-up.ps1") @args
