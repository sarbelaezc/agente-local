param(
    [ValidateSet("serve", "login", "pilot", "discover", "scan", "summary", "scheduled", "check-config")]
    [string]$Command = "serve",
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "MonthlyCloseAgent"),
    [string]$Start,
    [string]$End,
    [string]$Month,
    [int]$Port = 8766,
    [switch]$ReadOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Falta .venv. Ejecuta scripts\bootstrap.ps1."
}
$env:PYTHONPATH = Join-Path $root "src"
$arguments = @("-m", "monthly_close.main", $Command, "--data-dir", $DataDir, "--port", $Port)
if ($Start) { $arguments += @("--start", $Start) }
if ($End) { $arguments += @("--end", $End) }
if ($Month) { $arguments += @("--month", $Month) }
if ($ReadOnly) { $arguments += "--read-only" }
& $python @arguments
exit $LASTEXITCODE
