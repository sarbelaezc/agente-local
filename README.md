# Agente local

Agente personal que corre en tu equipo Windows para dos tareas financieras:

- **Cierre mensual de gastos:** lee los avisos del banco en el Outlook de
  Santiago y el Gmail de Manuela, propone cada movimiento y te deja decidir en un
  panel local cuánto fue gasto personal, familiar, ingreso o inversión.
- **Facturas electrónicas:** encuentra las facturas que llegan al correo de
  Hotmail, las registra en tu Excel y archiva cada correo en su carpeta.

Cómo trabaja:

- **Todo es local.** La inteligencia artificial es [Ollama](https://ollama.com)
  en tu equipo; ningún correo se envía a servicios de IA externos.
- **Tú apruebas.** Nada se escribe en el Excel, se mueve en el buzón ni cuenta
  como gasto sin tu aprobación en el panel. Lo que propone el modelo es solo una
  sugerencia y se valida contra el texto original.
- **Tus datos se quedan en el equipo.** Viven en la carpeta `.local`, que Git
  ignora (ver [Datos locales y privacidad](#datos-locales-y-privacidad)).

## Funcionalidades

| Funcionalidad | Estado | Comando principal | Guía |
|---|---|---|---|
| Cierre mensual de gastos | En uso con datos reales de 2026. Pendiente: primera ejecución de la tarea programada | `.\scripts\agente-local.ps1 import` | [docs/cierre-mensual.md](docs/cierre-mensual.md) |
| Facturas electrónicas de Hotmail | En uso con el buzón real | `.\scripts\agente-local.ps1 invoices` | [docs/facturas.md](docs/facturas.md) |

Lo verificado y lo pendiente de cada una está en la sección **Estado** de su guía.

## Inicio rápido

Requisitos: Windows, Git, Python 3.12 o posterior y Ollama.

1. Clona el repositorio y prepara el entorno (crea `.venv`, instala las
   dependencias y el navegador de las pruebas):

   ```powershell
   git clone <URL_DEL_REPOSITORIO>
   Set-Location agente-local
   .\scripts\bootstrap.ps1
   ```

2. Descarga el modelo, una sola vez: `ollama pull qwen2.5:7b`. No hace falta
   iniciar Ollama a mano; el lanzador lo hace.
3. Comprueba la instalación sin credenciales ni datos personales:
   `.\.venv\Scripts\python.exe -m pytest`.
4. Configura la funcionalidad que vayas a usar siguiendo
   [docs/configuracion.md](docs/configuracion.md): la app de Microsoft (común a
   ambas), los archivos locales y, si aplica, el Gmail de Manuela y el OCR.
5. Primer uso:

   ```powershell
   # Cierre mensual
   .\scripts\agente-local.ps1 login                    # sesión de Microsoft
   .\scripts\agente-local.ps1                          # panel: completa el paso 0
   .\scripts\agente-local.ps1 import                   # lee los avisos y abre el panel

   # Facturas
   .\scripts\agente-local.ps1 login -Mailbox facturas  # sesión de Microsoft de facturas
   .\scripts\agente-local.ps1 invoices                 # revisa y aprueba un lote
   ```

## Comandos del lanzador

`scripts\agente-local.ps1` es la entrada única. Inicia Ollama solo cuando el
comando lo necesita (y al terminar cierra únicamente la instancia que abrió),
guarda un [registro de cada ejecución](#datos-locales-y-privacidad) y traduce
estas opciones a los comandos de Python.

| Comando | Qué hace | Ollama |
|---|---|---|
| `serve [-Red]` (predeterminado) | Abre el panel del cierre mensual; con `-Red`, también para tus [dispositivos emparejados](docs/configuracion.md#11-paneles-desde-otros-dispositivos-opcional) | Opcional: solo los botones de sugerencias |
| `login [-Mailbox <buzón>]` | Inicia sesión: sin `-Mailbox`, el Outlook de Santiago; `-Mailbox manuela`, su Gmail; `-Mailbox facturas`, el buzón de facturas | No |
| `check-config` | Comprueba el paso 0 y la configuración del cierre | No |
| `import [-Start AAAA-MM-DD -End AAAA-MM-DD]` | `check-config`, `scan` y apertura del panel | Sí |
| `scan [-Start … -End …] [-ReadOnly] [-ReprocessEmpty]` | Lee los buzones: sin fechas, solo lo nuevo; con fechas, el rango completo | Sí, salvo con `-ReadOnly` |
| `pilot [-ReadOnly]` | Prueba la lectura con un solo aviso | Sí, salvo con `-ReadOnly` |
| `summary -Month AAAA-MM [-Owner santiago\|manuela\|hogar]` | Resumen de un mes en la terminal | No |
| `reconcile-statements [-Start AAAA-MM-DD] [-ReadOnly]` | Concilia los extractos PDF del banco y de Protección | Opcional: sugiere categorías de compras; sin él, solo reglas e historial |
| `import-manuela [-SourceDir <carpeta>]` | Importa los movimientos exportados del panel de Manuela | No |
| `owner-account -Owner <titular> -Suffix NNNN` | Registra una cuenta (últimos 4 dígitos) de un titular | No |
| `payroll-deduction -Owner <titular> -Suffix NNNN` | Marca un crédito cuyas cuotas se descuentan de la nómina | No |
| `import-credit-ledger` | Importa los pagos de los créditos desde tu Excel | No |
| `merge-category -FromCategory <a> -ToCategory <b>` | Une una categoría de gasto a otra | No |
| `backfill-destinations` | Completa el destino de pagos ya importados de todos los buzones, con la llave Bre-B o QR completa | No |
| `backfill-card-accounts [-ReadOnly]` | Corrige la tarjeta de movimientos de Santiago ya importados según los dígitos del aviso | No |
| `scheduled` | Cierre automático de la tarea programada | Solo si toca cerrar |
| `invoices [-BatchSize 1-20] [-Days 1-365] [-Resume] [-Red]` | Lote de facturas: 10 correos de los últimos 90 días por defecto; con `-Red`, el panel también para tus dispositivos emparejados | Sí |
| `invoices-cleanup [-OlderThanDays N] [-Apply]` | Limpia estados de lotes que ya no hace falta guardar | No |
| `devices [-Revoke <nombre> \| -RevokeAll]` | Lista o revoca los dispositivos emparejados con los paneles | No |
| `lan-firewall [-Remove]` | Como administrador, crea (o quita) la regla del firewall que deja entrar a los paneles desde tu red privada | No |

Si la política de PowerShell bloquea los scripts, ejecútalo así desde CMD o Git
Bash:
`powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File scripts\agente-local.ps1 <comando>`.
También puedes habilitarlo solo para la terminal actual con
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`.

## Cierre mensual de gastos

- **Alcance.** Lee en solo lectura los avisos del banco del Outlook de Santiago
  (Microsoft Graph) y del Gmail de Manuela (API de Gmail), además de los extractos
  PDF del banco y de Protección. Ollama propone los movimientos; tú decides en el
  panel si cada uno es gasto personal, familiar, ingreso, inversión o no cuenta.
  El panel muestra el mes, el año y los pendientes por titular (Santiago, Manuela
  u Hogar).
- **Configuración.** `.local\MonthlyCloseAgent\runtime.json` (app de Microsoft,
  buzones y Ollama), el paso 0 del panel (cuentas, categorías y remitentes
  aprobados) y, opcionalmente, `statements.json` y `credit_ledgers.json`. Ver
  [docs/configuracion.md](docs/configuracion.md).
- **Uso.** `import` para leer y revisar; la tarea programada cierra el mes el
  último día a las 22:00. Los extractos se buscan en todos los buzones y cada uno
  se concilia con los movimientos de su titular. Cuando los extractos de los dos
  cuadran y no queda nada pendiente, el mes se marca solo como **Conciliado**. Si
  aparece una tarjeta de crédito con un número nuevo, el panel pregunta si
  reemplaza a otra tarjeta o si es una tarjeta nueva. Ver
  [docs/cierre-mensual.md](docs/cierre-mensual.md).
- **Seguridad.** El cliente de Graph solo hace peticiones `GET` y el de Gmail
  usa el permiso `gmail.readonly`: nunca envía, mueve ni marca correos. Solo abre
  mensajes de remitentes aprobados y no guarda cuerpos de correo. Toda decisión
  queda auditada.

## Facturas electrónicas

- **Alcance.** Busca en la Bandeja de entrada de Hotmail los correos con
  facturas (XML, PDF o ZIP), extrae sus datos (XML de la DIAN, texto del PDF,
  OCR y Ollama para lo que falte), propone la carpeta y, tras tu aprobación,
  registra cada factura en el Excel y mueve el correo.
- **Configuración.** Sección `invoices` de `config\settings.yaml`: app de
  Microsoft, libro de Excel, OCR, Ollama, remitentes excluidos y aprobador. Ver
  [docs/configuracion.md](docs/configuracion.md).
- **Uso.** `invoices` abre el panel de revisión con un lote; `invoices -Resume`
  retoma lo aprobado. Ver [docs/facturas.md](docs/facturas.md).
- **Seguridad.** Aunque la app tiene `Mail.ReadWrite`, el cliente solo lee y
  mueve correos; borrar, enviar o marcar como leído se rechaza en el código. Cada
  correo se mueve solo después de verificar su registro en el Excel, y si su
  cuerpo cambió desde la aprobación, se bloquea.

## Datos locales y privacidad

Todo lo que el agente guarda vive en `.local` dentro del proyecto, que Git
ignora. Para guardarlo en otro lugar, define la variable de entorno
`AGENTE_LOCAL_DATA` con esa carpeta.

| Ruta | Contenido |
|---|---|
| `.local\MonthlyCloseAgent\` | `runtime.json`, `close.sqlite3` (movimientos, decisiones, reglas y auditoría), sesiones de Microsoft y Google, `statements.json` (contraseñas de los extractos de cada titular), `credit_ledgers.json` y `last-close.txt` |
| `.local\MailInvoiceAgent\` | Sesión de Microsoft de facturas, estados de lotes, decisiones de carpetas, copias del Excel (las 5 más recientes) y `tessdata` |
| `.local\ImportManuela\` | Archivos exportados del panel de Manuela |
| `.local\red-local\` | Solo si usas `-Red`: el certificado de la red de casa y su clave, el certificado del servidor y los dispositivos emparejados de cada panel (solo un resumen de su sesión) |
| `…\logs\` (en cada carpeta) | Un registro por ejecución; se conservan los 10 más recientes |
| `config\settings.yaml` | Configuración de facturas (también ignorada por Git) |

- **Sesiones y contraseñas.** El agente nunca ve tus contraseñas: inicias sesión
  en el navegador del sistema. Las sesiones son archivos locales; borrarlos cierra
  la sesión.
- **Registros de ejecución.** Guardan lo que el programa muestra (pasos,
  cantidades, el motivo de una parada), nunca cuerpos de correo, tokens ni
  contraseñas. Los modos de facturas que listan remitentes o datos de facturas no
  copian ese listado al registro.
- **Carpetas sincronizadas.** Si el proyecto está en OneDrive u otra carpeta
  sincronizada, `.local` se sincroniza con ella. Usa `AGENTE_LOCAL_DATA` con una
  ruta fuera de esa carpeta si no quieres que tus datos y sesiones salgan del
  equipo.
- **Sin cifrado propio.** Usa una cuenta de Windows protegida y cifrado de disco.
  Otros procesos del equipo pueden acceder a los paneles locales.
- **Otros dispositivos.** Los paneles solo escuchan en tu equipo, salvo que los
  abras con `-Red`. Entonces también responden por HTTPS en tu red privada, pero
  solo a los dispositivos que emparejas con un código que aparece en la terminal;
  sus cambios quedan registrados con el nombre del dispositivo. Ver
  [docs/configuracion.md](docs/configuracion.md#11-paneles-desde-otros-dispositivos-opcional).
- **Borrar datos.** Con el agente cerrado, borra la carpeta de la funcionalidad
  en `.local`. En facturas, `invoices-cleanup` borra solo los estados de lotes
  que ya no hacen falta.

## Desarrollo

Arquitectura, pruebas, asistentes de código (GitHub Copilot y Claude Code) y
flujo de ramas: [docs/desarrollo.md](docs/desarrollo.md). Las reglas que siguen
los asistentes están en [AGENTS.md](AGENTS.md).

## Aviso

Proyecto personal y experimental, sin relación con Microsoft ni con Google. Usa
sus API oficiales con tu propia cuenta: eres responsable de cumplir sus términos
de servicio. Nunca subas al repositorio tu configuración personal, libros de
Excel, facturas ni bases de datos.

## Licencia

MIT. Ver [LICENSE](LICENSE).
