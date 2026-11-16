# Lanzador unico: cierre mensual y facturas. Inicia Ollama solo si el comando lo
# necesita y no esta activo; al terminar cierra unicamente el proceso que inicio.
param(
    [Parameter(Position = 0)]
    [ValidateSet(
        "serve", "login", "check-config", "pilot", "scan", "import", "summary", "scheduled", "invoices",
        "import-manuela", "owner-account", "backfill-destinations", "reconcile-statements"
    )]
    [string]$Command = "serve",
    [string]$DataDir,
    [ValidateSet("santiago", "manuela", "hogar")]
    [string]$Owner,
    [ValidatePattern("^\d{4}$")]
    [string]$Suffix,
    [string]$SourceDir,
    [string]$Start,
    [string]$End,
    [string]$Month,
    [int]$Port = 8766,
    [switch]$ReadOnly,
    [switch]$ReprocessEmpty,
    [ValidateRange(1, 5)]
    [int]$BatchSize = 3,
    [ValidateRange(1, 365)]
    [int]$Days = 30,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
# $PSScriptRoot is not available yet when parameter defaults are evaluated with -File.
if (-not $DataDir) { $DataDir = Join-Path (Split-Path $PSScriptRoot -Parent) ".local\MonthlyCloseAgent" }
function Get-PanelContent {
    $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:8766")
    $request.Proxy = $null
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $response = $request.GetResponse()
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
}

function Start-SharedOllama {
    # Returns the process this script started, or $null when a running instance is reused.
    # OLLAMA_MODELS is left alone: Ollama uses its default store unless the user set one.
    param([string]$Endpoint)
    $uri = [Uri]$Endpoint
    $env:OLLAMA_HOST = $Endpoint
    $env:OLLAMA_NUM_PARALLEL = "1"
    $probe = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $probe.BeginConnect($uri.Host, $uri.Port, $null, $null)
        if (-not $connection.AsyncWaitHandle.WaitOne(3000)) {
            throw "El puerto de Ollama no respondio a tiempo."
        }
        try {
            $probe.EndConnect($connection)
            $running = $true
        } catch [System.Net.Sockets.SocketException] {
            if ($_.Exception.SocketErrorCode -ne [System.Net.Sockets.SocketError]::ConnectionRefused) {
                throw
            }
            $running = $false
        }
    } finally {
        $probe.Close()
    }
    if ($running) {
        Write-Host "Usando Ollama existente; no se cerrara al terminar."
        return $null
    }
    $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (-not (Test-Path -LiteralPath $ollama)) {
        $found = Get-Command ollama.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $found) { throw "No se encuentra Ollama." }
        $ollama = $found.Source
    }
    Write-Host "Iniciando Ollama compartido para facturas y cierre mensual."
    return Start-Process -FilePath $ollama -ArgumentList "serve" -NoNewWindow -PassThru
}

function Wait-OllamaModels {
    # Returns the installed models once the endpoint answers.
    param([string]$Endpoint, $Process)
    $deadline = (Get-Date).AddSeconds(30)
    while ($true) {
        if ($Process -and $Process.HasExited) { throw "Ollama termino antes de estar listo." }
        try {
            return (Invoke-RestMethod -Uri "$Endpoint/api/tags" -TimeoutSec 2).models
        } catch [System.Net.WebException] {
            if (-not $Process -or (Get-Date) -ge $deadline) {
                throw "Ollama no respondio correctamente; no se inicia el comando."
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Falta .venv. Ejecuta scripts\bootstrap.ps1."
}
$previousEnvironment = @{}
foreach ($key in @("PYTHONPATH", "OLLAMA_HOST", "OLLAMA_NUM_PARALLEL")) {
    $previousEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
}
$ollamaProcess = $null
$exitCode = 1
Push-Location $root
try {
    $env:PYTHONPATH = Join-Path $root "src"

    if ($Command -eq "invoices") {
        $config = Join-Path $root "config\settings.yaml"
        if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
            Copy-Item (Join-Path $root "config\settings.example.yaml") $config
            throw "Se creo config\settings.yaml. Configura Opera, el libro e invoices.ollama y repite."
        }
        $endpoint = & $python -c (
            "import sys; from pathlib import Path; from mail_invoices.settings import load_settings; " +
            "print(load_settings(Path(sys.argv[1])).ollama_url)"
        ) $config
        if ($LASTEXITCODE -ne 0 -or -not $endpoint) { throw "No se pudo leer config\settings.yaml." }
        $endpoint = "$endpoint".Trim().TrimEnd("/")
        $ollamaProcess = Start-SharedOllama $endpoint
        Wait-OllamaModels $endpoint $ollamaProcess | Out-Null
        $mode = if ($Resume) { "--resume-invoice-batch" } else { "--process-invoice-batch" }
        & $python -m mail_invoices.main --config "config\settings.yaml" $mode `
            --batch-size "$BatchSize" --days "$Days"
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Write-Warning "El lote de facturas termino con codigo $exitCode. Revisa su informe antes de repetir."
        }
        exit $exitCode
    }

    $needsOllama = $Command -eq "import" -or ($Command -in @("scan", "pilot") -and -not $ReadOnly)
    if ($Command -eq "scheduled") {
        # The hourly task only reads the mailbox in the closing hour; skip Ollama otherwise.
        & $python -c (
            "import sys; from datetime import datetime; from pathlib import Path; " +
            "from zoneinfo import ZoneInfo; from monthly_close.main import scheduled_month; " +
            "from monthly_close.store import Store; " +
            "p = Store(Path(sys.argv[1]) / 'close.sqlite3').preferences(); " +
            "sys.exit(0 if scheduled_month(datetime.now(ZoneInfo(p.timezone)), p.timezone) else 3)"
        ) $DataDir
        if ($LASTEXITCODE -eq 3) { exit 0 }
        $needsOllama = $LASTEXITCODE -eq 0
    }
    if ($Command -eq "import") {
        & $python -m monthly_close.main check-config --data-dir $DataDir
        if ($LASTEXITCODE -ne 0) { throw "Configuracion no validada; no se inicia la importacion." }
    }
    if ($needsOllama) {
        $settings = Get-Content -LiteralPath (Join-Path $DataDir "runtime.json") -Raw | ConvertFrom-Json
        $endpoint = $settings.ollama_url.TrimEnd("/")
        $ollamaProcess = Start-SharedOllama $endpoint
        $models = Wait-OllamaModels $endpoint $ollamaProcess
        if (-not ($models | Where-Object { $_.name -ceq $settings.ollama_model })) {
            throw "El modelo configurado no esta instalado. No se descargara automaticamente."
        }
    }

    $pythonCommand = if ($Command -eq "import") { "scan" } else { $Command }
    $arguments = @("-u", "-m", "monthly_close.main", $pythonCommand, "--data-dir", $DataDir, "--port", $Port)
    if ($Start) { $arguments += @("--start", $Start) }
    if ($End) { $arguments += @("--end", $End) }
    if ($Month) { $arguments += @("--month", $Month) }
    if ($Owner) { $arguments += @("--owner", $Owner) }
    if ($Suffix) { $arguments += @("--suffix", $Suffix) }
    if ($SourceDir) { $arguments += @("--source-dir", $SourceDir) }
    if ($ReadOnly) { $arguments += "--read-only" }
    if ($ReprocessEmpty) { $arguments += "--reprocess-empty" }
    if ($Command -eq "import") {
        Write-Host "Iniciando recorrido del buzon."
    }
    & $python @arguments
    $exitCode = $LASTEXITCODE

    if ($Command -eq "import" -and -not $ReadOnly) {
        $panelUrl = "http://127.0.0.1:8766"
        $panelProcess = $null
        try {
            $panel = $null
            try {
                $panel = Get-PanelContent
            } catch [System.Net.WebException] {
                if ($_.Exception.Status -ne [System.Net.WebExceptionStatus]::ConnectFailure) {
                    throw "El puerto del panel no responde correctamente."
                }
            }
            if ($null -eq $panel) {
                $panelProcess = Start-Process -FilePath $python -ArgumentList @(
                    "-u", "-m", "monthly_close.main", "serve", "--data-dir", "`"$DataDir`""
                ) -WorkingDirectory $root -PassThru
                $panelDeadline = (Get-Date).AddSeconds(15)
                while ($null -eq $panel) {
                    if ($panelProcess.HasExited -or (Get-Date) -ge $panelDeadline) {
                        throw "El panel no pudo iniciarse."
                    }
                    try {
                        $panel = Get-PanelContent
                    } catch [System.Net.WebException] {
                        if ($_.Exception.Status -ne [System.Net.WebExceptionStatus]::ConnectFailure) {
                            throw
                        }
                        Start-Sleep -Milliseconds 250
                    }
                }
            }
            if ($panel -notmatch '<title>Cierre mensual') {
                throw "El puerto 8766 esta ocupado por una pagina distinta al panel."
            }
            Start-Process -FilePath $panelUrl
            Write-Host "Panel abierto. Manten su terminal abierta para revisar los gastos."
        } catch {
            if ($panelProcess -and -not $panelProcess.HasExited) {
                Stop-Process -Id $panelProcess.Id -ErrorAction Stop
            }
            Write-Warning "No se pudo abrir el panel: $($_.Exception.Message)"
            if ($exitCode -eq 0) { $exitCode = 1 }
        }
    }
} finally {
    if ($ollamaProcess -and -not $ollamaProcess.HasExited) {
        Write-Host "Cerrando la instancia de Ollama iniciada por este script."
        Stop-Process -Id $ollamaProcess.Id -ErrorAction Stop
        $ollamaProcess.WaitForExit()
    }
    foreach ($key in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previousEnvironment[$key], "Process")
    }
    Pop-Location
}
exit $exitCode
