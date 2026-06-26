$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw ".NET SDK is required. Install .NET 8 SDK and retry." }
if (-not (Test-Path ".\zapretvless.sln")) { throw "zapretvless.sln was not found." }

dotnet restore .\zapretvless.sln
dotnet build .\zapretvless.sln
dotnet test .\zapretvless.sln
