param(
  [ValidateRange(1, 5)]
  [int]$BatchSize = 3,
  [ValidateRange(1, 365)]
  [int]$Days = 30,
  [switch]$Resume
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$config = Join-Path $repoRoot "config\settings.yaml"

if (-not (Test-Path $python -PathType Leaf)) {
  throw "Virtual environment not found. Run scripts\bootstrap.ps1 first."
}
if (-not (Test-Path $config -PathType Leaf)) {
  Copy-Item (Join-Path $repoRoot "config\settings.example.yaml") $config
  throw "Created config\settings.yaml. Configure Opera, workbook, and invoices.ollama before running again."
}

$mode = if ($Resume) { "--resume-invoice-batch" } else { "--process-invoice-batch" }
$pythonArgs = @(
  "-m", "mail_invoices.main",
  "--config", "config\settings.yaml",
  $mode,
  "--batch-size", "$BatchSize",
  "--days", "$Days"
)

Push-Location $repoRoot
try {
  & $python @pythonArgs
  if ($LASTEXITCODE -ne 0) {
    throw "Invoice batch exited with code $LASTEXITCODE. Review its report before retrying."
  }
}
finally {
  Pop-Location
}