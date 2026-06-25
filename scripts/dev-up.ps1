$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$IsWindowsHost = [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Windows)
if (-not $IsWindowsHost) { throw "dev-up.ps1 must be run on Windows because it starts the WPF UI." }
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw ".NET SDK is required. Install .NET 8 SDK and retry." }
if (-not (Test-Path ".\zapretvless.sln")) { throw "zapretvless.sln was not found." }

dotnet restore .\zapretvless.sln
dotnet build .\zapretvless.sln
dotnet test .\zapretvless.sln

$AppRoot = Join-Path $env:LOCALAPPDATA "ArbelinOne"
New-Item -ItemType Directory -Force -Path $AppRoot, (Join-Path $AppRoot "logs"), (Join-Path $AppRoot "configs") | Out-Null

$EngineFiles = @("engines\xray\xray.exe", "engines\zapret\winws.exe", "engines\zapret\winws2.exe", "engines\zapret\WinDivert64.sys")
foreach ($EngineFile in $EngineFiles) {
    if (-not (Test-Path $EngineFile)) { Write-Warning "Missing optional engine binary: $EngineFile" }
}

Write-Host "PR-01: Xray, Zapret, WinDivert, proxy, DNS, and routes are not started or changed."
dotnet run --project .\src\Arbelin.One.Client\Arbelin.One.Client.csproj
