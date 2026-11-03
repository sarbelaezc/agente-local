param(
    [string]$Start,
    [string]$End,
    [switch]$ReadOnly
)

$ErrorActionPreference = "Stop"
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
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
$data = Join-Path $env:LOCALAPPDATA "MonthlyCloseAgent"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Falta .venv. Prepara el entorno antes de importar."
}
$previousEnvironment = @{}
foreach ($key in @("PYTHONPATH", "OLLAMA_HOST", "OLLAMA_MODELS", "OLLAMA_NUM_PARALLEL")) {
    $previousEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
}
$ollamaProcess = $null
$exitCode = 1
try {
    $env:PYTHONPATH = Join-Path $root "src"
    & $python -m monthly_close.main check-config
    if ($LASTEXITCODE -ne 0) { throw "Configuracion no validada; no se inicia la importacion." }
    $settings = Get-Content -LiteralPath (Join-Path $data "runtime.json") -Raw | ConvertFrom-Json
    $endpoint = $settings.ollama_url.TrimEnd("/")
    $uri = [Uri]$endpoint
    $env:OLLAMA_HOST = $endpoint
    $env:OLLAMA_MODELS = Join-Path $data "OllamaModels"
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
        Write-Output "Usando Ollama existente; no se cerrara al terminar."
    } else {
        $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
        if (-not (Test-Path -LiteralPath $ollama)) { throw "No se encuentra Ollama." }
        Write-Output "Iniciando Ollama exclusivo para cierre mensual."
        $ollamaProcess = Start-Process -FilePath $ollama -ArgumentList "serve" -NoNewWindow -PassThru
    }
    $deadline = (Get-Date).AddSeconds(30)
    while ($true) {
        if ($ollamaProcess -and $ollamaProcess.HasExited) {
            throw "Ollama termino antes de estar listo."
        }
        try {
            $payload = Invoke-RestMethod -Uri "$endpoint/api/tags" -TimeoutSec 2
            break
        } catch [System.Net.WebException] {
            if (-not $ollamaProcess -or (Get-Date) -ge $deadline) {
                throw "Ollama no respondio correctamente; no se inicia la importacion."
            }
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not ($payload.models | Where-Object { $_.name -ceq $settings.ollama_model })) {
        throw "El modelo configurado no esta instalado. No se descargara automaticamente."
    }
    $arguments = @("-u", "-m", "monthly_close.main", "scan")
    if ($Start) { $arguments += @("--start", $Start) }
    if ($End) { $arguments += @("--end", $End) }
    if ($ReadOnly) { $arguments += "--read-only" }
    Write-Output "Iniciando recorrido. No interactues con el perfil de Edge."
    & $python @arguments
    $exitCode = $LASTEXITCODE
    if (-not $ReadOnly) {
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
                    "-u", "-m", "monthly_close.main", "serve"
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
            Write-Output "Panel abierto. Manten su terminal abierta para revisar los gastos."
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
        Write-Output "Cerrando la instancia de Ollama iniciada por este script."
        Stop-Process -Id $ollamaProcess.Id -ErrorAction Stop
        $ollamaProcess.WaitForExit()
    }
    foreach ($key in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previousEnvironment[$key], "Process")
    }
}
exit $exitCode
