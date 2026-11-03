[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "MonthlyCloseAgent"),
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$name = "AgenteLocal-MonthlyClose"
if ($Remove) {
    if ($PSCmdlet.ShouldProcess($name, "Eliminar tarea")) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    return
}
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Falta .venv." }
$env:PYTHONPATH = Join-Path $root "src"
& $python -m monthly_close.main check-config --data-dir $DataDir
if ($LASTEXITCODE -ne 0) { throw "Completa y verifica primero la configuracion local." }
$runner = Join-Path $PSScriptRoot "run-monthly-close.ps1"
if ($runner.Contains('"') -or $DataDir.Contains('"')) { throw "Ruta no valida." }
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    '-NoProfile -File "{0}" -Command scheduled -DataDir "{1}"' -f $runner, $DataDir
) -WorkingDirectory $root
# The hourly wake-up delegates timezone/DST and last-day checks to Python's ZoneInfo.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
if ($PSCmdlet.ShouldProcess($name, "Registrar cierre mensual persistente")) {
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description (
            "Comprueba la zona configurada cada hora. Solo lee Outlook el ultimo dia del mes a las 22:00. " +
            "Requiere sesion Windows abierta; nunca envia correo ni decide gastos."
        ) | Out-Null
    Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
}
