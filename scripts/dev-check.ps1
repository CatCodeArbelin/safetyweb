$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw ".NET SDK is required. Install .NET 8 SDK and retry." }
if (-not (Test-Path ".\zapretvless.sln")) { throw "zapretvless.sln was not found." }

$RequiredPaths = @(
    ".\zapretvless.sln",
    ".\src\Arbelin.One.Client",
    ".\src\Arbelin.One.Service",
    ".\src\Arbelin.One.Shared",
    ".\src\Arbelin.One.Tests",
    ".\up.ps1",
    ".\scripts\dev-up.ps1",
    ".\scripts\dev-check.ps1",
    ".\docker-compose.yml",
    ".\docs",
    ".\LICENSES",
    ".\engines\xray\.gitkeep",
    ".\engines\zapret\.gitkeep"
)
foreach ($RequiredPath in $RequiredPaths) {
    if (-not (Test-Path $RequiredPath)) { throw "Required bootstrap path was not found: $RequiredPath" }
}

dotnet restore .\zapretvless.sln
dotnet build .\zapretvless.sln
dotnet test .\zapretvless.sln

Write-Host "Safe check complete. UI, service, Xray, Zapret, WinDivert, proxy, DNS, and routes were not started or changed."
