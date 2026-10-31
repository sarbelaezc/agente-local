$ErrorActionPreference = "Stop"
if (-not (Test-Path "config/settings.yaml")) {
  Copy-Item "config/settings.example.yaml" "config/settings.yaml"
  Write-Host "Created config/settings.yaml. Add explicitly authorized chats before continuing."
}
.venv\Scripts\python.exe -m app.main --config config/settings.yaml
