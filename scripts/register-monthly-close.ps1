[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$DataDir,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
# $PSScriptRoot is not available yet when parameter defaults are evaluated with -File.
# Same folder as shared.storage.data_root().
$localData = if ("$env:AGENTE_LOCAL_DATA".Trim()) { "$env:AGENTE_LOCAL_DATA".Trim() } else { Join-Path (Split-Path $PSScriptRoot -Parent) ".local" }
if (-not $DataDir) { $DataDir = Join-Path $localData "MonthlyCloseAgent" }
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
$runner = Join-Path $PSScriptRoot "agente-local.ps1"
if ($runner.Contains('"') -or $DataDir.Contains('"')) { throw "Ruta no valida." }

function ConvertTo-XmlText([string]$Value) { [System.Security.SecurityElement]::Escape($Value) }

# Last day of every month at 22:00. StartWhenAvailable runs a missed close as soon as the
# computer is on and the session is open; Python decides which month is due, so a late
# or repeated run never closes a month twice.
$arguments = '-NoProfile -ExecutionPolicy RemoteSigned -File "{0}" scheduled -DataDir "{1}"' -f $runner, $DataDir
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$start = (Get-Date).ToString("yyyy-MM-dd") + "T22:00:00"
$months = (
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December"
) | ForEach-Object { "<$_ />" }
$description = (
    "Cierre mensual: el ultimo dia de cada mes a las 22:00, o al encender el equipo si esa hora se perdio. " +
    "Lee los buzones en solo lectura, concilia extractos y escribe el resumen; nunca envia correo ni decide gastos. " +
    "Si la sesion de Gmail vencio, abre el inicio de sesion de Google y espera 15 minutos. Requiere sesion Windows abierta."
)
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$(ConvertTo-XmlText $description)</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth><Day>Last</Day></DaysOfMonth>
        <Months>$($months -join '')</Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(ConvertTo-XmlText $user)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$(ConvertTo-XmlText $arguments)</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText $root)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
[xml]$xml | Out-Null  # a malformed definition stops here, before anything is registered
if ($PSCmdlet.ShouldProcess($name, "Registrar cierre mensual persistente")) {
    Register-ScheduledTask -TaskName $name -Xml $xml -Force | Out-Null
    Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
    Get-ScheduledTaskInfo -TaskName $name | Select-Object NextRunTime
} else {
    $xml
}
