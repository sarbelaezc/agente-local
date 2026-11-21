# Configuración

Guía para dejar el agente listo en un equipo Windows. Se sigue una vez; después
solo se vuelve a ella para cambiar algo. Todos los comandos se ejecutan desde la
raíz del proyecto en PowerShell.

- [1. Instalar el proyecto](#1-instalar-el-proyecto)
- [2. Ollama](#2-ollama)
- [3. App de Microsoft (ambas funcionalidades)](#3-app-de-microsoft-ambas-funcionalidades)
- [4. Cierre mensual: runtime.json y paso 0](#4-cierre-mensual-runtimejson-y-paso-0)
- [5. Gmail de Manuela (opcional)](#5-gmail-de-manuela-opcional)
- [6. Extractos y créditos (opcional)](#6-extractos-y-créditos-opcional)
- [7. Facturas: settings.yaml](#7-facturas-settingsyaml)
- [8. OCR para PDF escaneados (opcional)](#8-ocr-para-pdf-escaneados-opcional)
- [9. Tarea programada del cierre (opcional)](#9-tarea-programada-del-cierre-opcional)
- [10. Carpeta de datos](#10-carpeta-de-datos)
- [11. Paneles desde otros dispositivos (opcional)](#11-paneles-desde-otros-dispositivos-opcional)

## 1. Instalar el proyecto

1. Instala Git, Python 3.12 o posterior y Ollama.
2. Clona el repositorio y ejecuta el script de preparación. Crea `.venv`, instala
   el proyecto con sus dependencias de desarrollo y descarga Chromium para las
   pruebas del panel:

   ```powershell
   git clone <URL_DEL_REPOSITORIO>
   Set-Location agente-local
   .\scripts\bootstrap.ps1
   ```

3. Comprueba que todo funciona, sin credenciales, correos ni libros personales:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest
   ```

   La configuración de pytest usa captura `sys`; no la cambies a `fd`, porque
   las pruebas gráficas de Windows fallan.

Para actualizar las dependencias más adelante:
`.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`.

## 2. Ollama

Las dos funcionalidades comparten **una sola instancia** de Ollama en
`http://127.0.0.1:11434` y el mismo modelo, `qwen2.5:7b`. Descárgalo una vez:

```powershell
ollama pull qwen2.5:7b
```

No hace falta iniciar Ollama a mano. El lanzador reutiliza la instancia activa o,
si no hay ninguna, la inicia y al terminar cierra solo la que abrió. Nunca
descarga modelos. Usa el almacén de modelos predeterminado de Ollama; si defines
`OLLAMA_MODELS`, se respeta (ver la
[guía de Ollama](https://docs.ollama.com/faq#how-do-i-configure-ollama-server)).

El endpoint y el modelo se configuran en dos sitios que deben coincidir:
`ollama_url` y `ollama_model` de `runtime.json` (cierre mensual) e
`invoices.ollama` de `settings.yaml` (facturas). Solo se aceptan direcciones
locales (`127.0.0.1`, `localhost` o `::1`).

Se evaluó `qwen3.5:9b` para facturas y se descartó: dio las mismas respuestas,
pero tardó 76,7 s en completar campos y 104,2 s en clasificar, frente a 7,4 s y
2,8 s con `qwen2.5:7b`, por su razonamiento previo a la respuesta.

## 3. App de Microsoft (ambas funcionalidades)

Las dos funcionalidades usan **una misma app registrada** en Azure. Registrarla
y consultar correo no tiene costo.

1. Si tu cuenta personal de Microsoft no tiene directorio de Microsoft Entra, el
   portal muestra el error `AADSTS16000`. Crea antes una cuenta gratuita de Azure,
   que crea ese directorio.
2. En [portal.azure.com](https://portal.azure.com): Microsoft Entra ID, App
   registrations, New registration.
   - Tipos de cuenta: debe admitir cuentas personales de Microsoft.
   - Redirect URI: plataforma «Aplicaciones móviles y de escritorio» con
     `http://localhost`.
3. En **API permissions**, agrega los permisos delegados de Microsoft Graph
   `Mail.Read` (cierre mensual) y `Mail.ReadWrite` (facturas, que mueve correos).
4. No crees secretos de cliente. Copia el `Application (client) ID`: no es un
   secreto y va en `runtime.json` y en `settings.yaml`.

**Por qué es seguro compartirla.** Como la app tiene `Mail.ReadWrite`, los tokens
de cualquiera de las dos funcionalidades pueden llevar ese permiso. Por eso cada
cliente limita en su código lo que hace: el del cierre mensual solo envía `GET`,
y el de facturas solo lee y mueve. Cada funcionalidad guarda su propia sesión, y
los tokens solo se envían a `graph.microsoft.com/v1.0`.

## 4. Cierre mensual: runtime.json y paso 0

1. Crea la configuración técnica y abre el panel:

   ```powershell
   $data = ".local\MonthlyCloseAgent"
   New-Item -ItemType Directory -Force $data | Out-Null
   if (-not (Test-Path (Join-Path $data "runtime.json"))) {
     Copy-Item config\monthly-close.example.json (Join-Path $data "runtime.json")
   }
   .\scripts\agente-local.ps1
   ```

2. En el panel, completa el **paso 0**: ubicación y zona horaria, alias de tus
   cuentas, cuentas excluidas, etiqueta para transferencias, tratamiento de ahorros
   y de transferencias a personas, moneda, categorías, remitentes exactos de los
   avisos del banco y autorización para contenido confidencial. Usa alias: no
   escribas números de cuenta, contraseñas ni tokens. Sin este paso la lectura
   queda bloqueada. Las categorías no vienen precargadas; una lista inicial
   recomendada es `Domicilios y restaurantes`, `Compras`, `Salud y farmacia`,
   `Transporte`, `Suscripciones`, `Mercado y tiendas` y `Otros gastos`.
3. Completa `runtime.json`:

   ```json
   {
     "graph_client_id": "<Application (client) ID>",
     "graph_authority": "consumers",
     "ollama_url": "http://127.0.0.1:11434",
     "ollama_model": "qwen2.5:7b",
     "max_messages": 200,
     "folder_scan_verified": false
   }
   ```

   | Clave | Uso |
   |---|---|
   | `graph_authority` | `consumers` (por defecto, cuentas personales) o `common` |
   | `max_messages` | Máximo de filas que lista un recorrido, sumando todas las carpetas (hasta 10000). Si se supera, se detiene sin saltarse carpetas. Una primera sincronización grande puede necesitar más |
   | `folder_scan_verified` | `true` solo después de revisar el paso 5; mientras sea `false` no se importa nada |
   | `mailboxes` | Lista de buzones; ver [Gmail de Manuela](#5-gmail-de-manuela-opcional). Sin ella se lee solo el Outlook de Santiago |

4. Inicia sesión. Se abre la página de Microsoft en el navegador del sistema; el
   agente no ve tu contraseña ni interviene en MFA o captcha:

   ```powershell
   .\scripts\agente-local.ps1 login
   ```

5. **Valida el recorrido antes de importar.** Este comando lista solo metadatos
   (no abre mensajes ni usa Ollama) y comprueba que cada carpeta coincida con su
   total:

   ```powershell
   .\scripts\agente-local.ps1 scan -ReadOnly -Start 2026-10-01 -End 2026-11-01
   ```

   Si todo cuadra, cambia `folder_scan_verified` a `true`. Luego sigue con
   [Primer uso](cierre-mensual.md#primer-uso) en la guía del cierre.

## 5. Gmail de Manuela (opcional)

**Crear el cliente de Google** (una vez, con la cuenta de Manuela o la tuya), en
[console.cloud.google.com](https://console.cloud.google.com):

1. Crea un proyecto y habilita la **Gmail API**.
2. En la pantalla de consentimiento de OAuth elige tipo **Externo** y añade a
   Manuela como usuaria de prueba.
3. En Credenciales, crea un **ID de cliente de OAuth** de tipo **App de
   escritorio** y descarga el JSON como
   `.local\MonthlyCloseAgent\gmail_client_secret.json`. No lo subas a Git.

Con la app en estado **Testing**, Google emite sesiones que
[caducan a los 7 días](https://developers.google.com/identity/protocols/oauth2#expiration):
Manuela tendrá que volver a iniciar sesión casi en cada cierre. Pasarla a **In
production** sin verificar muestra la pantalla de app no verificada y limita la
app a 100 usuarios; no está comprobado si eso basta para `gmail.readonly` en uso
personal. Hoy la app está en Testing.

**Agregar el buzón** en `runtime.json`:

```json
{
  "graph_client_id": "<Application (client) ID>",
  "folder_scan_verified": true,
  "mailboxes": [
    {"name": "santiago", "provider": "graph", "owner": "santiago"},
    {"name": "manuela", "provider": "gmail", "owner": "manuela",
     "address": "<correo de Manuela>", "verified": false}
  ]
}
```

| Clave | Significado |
|---|---|
| `name` | Nombre del buzón (minúsculas, números y guiones). Se usa en `login -Mailbox`. `facturas` está reservado |
| `provider` | `graph` (Outlook) o `gmail` |
| `owner` | `santiago` o `manuela`: titular de todo lo que se importa desde ese buzón |
| `verified` | Solo Gmail. `true` tras revisar `scan -ReadOnly`; mientras sea `false` no se importa nada. Outlook usa `folder_scan_verified` |
| `address` | Solo Gmail. Correo exacto de la cuenta: se rechaza un inicio de sesión con otra. Necesario para que la tarea programada pueda pedir un nuevo inicio de sesión |
| `token_cache` | Opcional. Archivo de sesión (por defecto `graph_token_cache.bin` y `gmail_token_<name>.json`) |
| `client_secrets` | Opcional, solo Gmail. Por defecto `gmail_client_secret.json` |
| `origin` | Opcional. Forma parte de la identidad de cada mensaje (por defecto `outlook.live.com` y `mail.google.com`). No lo cambies después de importar: volvería a importar todo |
| `accounts`, `excluded_accounts` | Opcional. Alias de cuentas de ese titular para Ollama; si faltan, se usan los del paso 0 |

El Outlook de Santiago es obligatorio. El piloto lee solo ese buzón. Los remitentes aprobados del paso 0
valen para todos los buzones.

**Iniciar sesión y validar.** Manuela inicia sesión en su navegador; si Google
concede cualquier permiso distinto de `gmail.readonly`, la sesión no se guarda.

```powershell
.\scripts\agente-local.ps1 login -Mailbox manuela
.\scripts\agente-local.ps1 scan -ReadOnly -Start 2026-11-01 -End 2026-12-01
```

El recorrido de solo lectura muestra, por buzón, cuántos mensajes listó y cuántos
son de remitentes aprobados. Revísalo y cambia `verified` a `true`.

## 6. Extractos y créditos (opcional)

**Extractos del banco y de Protección.** Crea
`.local\MonthlyCloseAgent\statements.json`:

```json
{"passwords": {"santiago": "<contraseña de los PDF de Santiago>",
               "manuela": "<contraseña de los PDF de Manuela>"},
 "approved_senders": ["<remitente exacto del banco>", "afp_proteccion@proteccion.com"]}
```

Los extractos se buscan en **todos los buzones** configurados, y el banco cifra los
PDF de cada titular con su propia contraseña. `passwords` asigna una a cada
titular, y los PDF de un buzón se abren **solo con la contraseña de su titular**.
Si no la abre (por ejemplo, un extracto de otra persona reenviado a ese buzón), la
conciliación se detiene y dice cuál y de qué buzón, sin importar nada: nunca se
cuenta como del titular equivocado. Un buzón con extractos y sin contraseña de su
titular también la detiene. La clave antigua `password`, con una sola contraseña,
se toma como la de Santiago. Escribe tú las contraseñas en el archivo: el agente
nunca las pide.

Las contraseñas quedan en texto plano, fuera de Git, por decisión del usuario, y
nunca se imprimen. Solo se leen adjuntos de esos remitentes exactos. Uso en
[Extractos](cierre-mensual.md#conciliar-extractos).

**Registro de pagos de un crédito.** Si llevas tus pagos de un crédito en Excel,
descríbelo en `.local\MonthlyCloseAgent\credit_ledgers.json`:

```json
[{"owner": "santiago", "suffix": "NNNN", "name": "Nombre del crédito",
  "path": "C:\\ruta\\al\\libro.xlsx", "sheet": "Hoja1",
  "bonus_months": [6, 12], "category": "Transporte", "payroll": true,
  "product": "CREDITO", "contrast": "capital"}]
```

| Clave | Uso |
|---|---|
| `product` | `CREDITO` (consumo o vehículo) o `HIPOTECARIO` |
| `contrast` | `capital`: el capital del registro contra la baja del saldo de capital del extracto. `payments`: los pagos del registro contra los que lista el extracto en cada periodo (hipotecario, cuyo saldo incluye intereses) |
| `payroll` | `true` si las cuotas se descuentan de la nómina (por defecto `false`) |
| `bonus_months` | Meses de prima en que un abono a capital es descuento de nómina |
| `category` | Categoría de gasto configurada en el paso 0 |

Uso en [Créditos](cierre-mensual.md#créditos-y-nómina).

## 7. Facturas: settings.yaml

1. Crea la configuración si no existe (no copies la plantilla encima de una
   configuración tuya):

   ```powershell
   if (-not (Test-Path "config\settings.yaml")) {
     Copy-Item "config\settings.example.yaml" "config\settings.yaml"
   }
   ```

2. Completa la sección `invoices`:

   | Clave | Uso |
   |---|---|
   | `graph_client_id` | El mismo `Application (client) ID` de `runtime.json` |
   | `graph_authority` | `consumers` (por defecto) o `common` |
   | `workbook`, `sheet` | Libro de Excel y hoja (`Hoja1`) donde se registran las facturas |
   | `backup_dir` | Carpeta de copias del libro; se conservan las 5 más recientes |
   | `approver` | Nombre que se precarga en «Aprobado por» |
   | `ollama.url`, `ollama.model` | El Ollama compartido; deben coincidir con `runtime.json` |
   | `ocr` | Ruta de Tesseract, de `tessdata` e idiomas (`spa+eng`) |
   | `excluded_senders` | Direcciones exactas o `@dominio` cuyos correos nunca son facturas para registrar (por ejemplo, recibos en USD, porque el libro solo admite COP). Se archivan a mano |
   | `folder_rules` | Opcional. Carpeta por remitente exacto o `@dominio`; solo se usa cuando un correo no trae contenido que analizar |
   | `batch_limit` | Correos que abre `--classify-invoices` (no afecta los lotes) |
   | `attachments_dir` | Carpeta temporal de adjuntos; cada ejecución borra lo suyo |

   Las rutas relativas se resuelven desde la raíz del proyecto. Un archivo antiguo
   con los ajustes en la raíz (sin `invoices`) sigue funcionando; para migrarlo,
   agrúpalos dentro de `invoices`. Un bloque global `ollama` antiguo se usa como
   endpoint compartido.

3. Inicia sesión en el buzón de facturas:

   ```powershell
   .\scripts\agente-local.ps1 login -Mailbox facturas
   ```

   La sesión queda en `.local\MailInvoiceAgent\graph_token_cache.bin`. Los lotes
   nunca abren un navegador: si la sesión falta o venció, se detienen y piden
   iniciar sesión.

## 8. OCR para PDF escaneados (opcional)

Solo hace falta para facturas en PDF sin texto. Sin Tesseract, esas facturas
quedan para revisión manual.

1. `winget install --id UB-Mannheim.TesseractOCR -e`
2. Descarga `spa.traineddata` de
   [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) a
   `.local\MailInvoiceAgent\tessdata` y copia ahí `eng.traineddata` desde
   `C:\Program Files\Tesseract-OCR\tessdata`.
3. Ajusta `invoices.ocr` en `settings.yaml` si usas otras rutas.

## 9. Tarea programada del cierre (opcional)

Después de configurar y validar los buzones, instala la tarea en el Programador
de tareas de Windows (persiste aunque cierres VS Code):

```powershell
.\scripts\register-monthly-close.ps1 -WhatIf   # muestra lo que haría
.\scripts\register-monthly-close.ps1
Get-ScheduledTaskInfo -TaskName "AgenteLocal-MonthlyClose"
```

Para quitarla: `.\scripts\register-monthly-close.ps1 -Remove`. Qué hace y cuándo:
[Cierre y tarea programada](cierre-mensual.md#cierre-y-tarea-programada).

## 10. Carpeta de datos

Por defecto todo se guarda en `.local` dentro del proyecto. Para usar otra
carpeta (por ejemplo, fuera de OneDrive), define la variable de entorno
`AGENTE_LOCAL_DATA` con esa ruta y ajusta las rutas de `settings.yaml` que
apunten a `.local`. Las pruebas usan siempre una carpeta temporal y nunca leen ni
escriben `.local`.

Los estados de lotes de facturas dependen de la app (`graph_client_id`), el libro
y la hoja. Si cambias alguno, los lotes guardados dejan de reconocerse; la
[limpieza de lotes](facturas.md#limpiar-lotes) los borra.

## 11. Paneles desde otros dispositivos (opcional)

Los paneles escuchan solo en tu equipo. Con `-Red` también puedes usarlos desde
otro computador o un celular de la misma red de casa: consultar y decidir, igual
que en el equipo. Solo entran los dispositivos que emparejas tú.

**Una vez**

1. Comprueba que la red de tu casa está marcada como **privada** en Windows
   (Configuración → Red e Internet → la conexión → Tipo de perfil de red). No lo
   hagas en redes públicas, como las de un café.
2. Crea la regla del firewall desde una PowerShell abierta **como administrador**:

   ```powershell
   .\scripts\agente-local.ps1 lan-firewall
   ```

   Permite la entrada solo a los puertos 8767 (cierre) y 8768 (facturas), solo
   en redes privadas y solo desde tu subred. Mientras ningún panel se abre con
   `-Red`, nada escucha en esos puertos. Para quitarla: `lan-firewall -Remove`.

**Cada vez**

1. Abre el panel con `-Red`:

   ```powershell
   .\scripts\agente-local.ps1 serve -Red      # cierre mensual
   .\scripts\agente-local.ps1 invoices -Red   # lote de facturas
   ```

   La terminal muestra la dirección para los otros dispositivos (por ejemplo
   `https://192.168.1.20:8767/`) y la huella del certificado de la red de casa. Si
   el equipo no tiene una dirección de red privada, el comando se detiene sin
   abrir nada.
2. En el otro dispositivo abre esa dirección. La primera vez, el navegador avisa
   que no conoce el certificado (ver abajo).
3. Escribe un nombre para el dispositivo y pulsa **Pedir código**. El código
   aparece **solo en la terminal del computador**, junto con el nombre y la
   dirección del dispositivo que lo pidió. Escríbelo en el dispositivo y pulsa
   **Entrar**.

El código sirve una vez, vence a los 5 minutos y solo vale para el dispositivo
que lo pidió. Tras 5 códigos fallidos el emparejamiento se bloquea hasta reiniciar
el panel. Si aparece en la terminal una solicitud que no hiciste, no escribas el
código y cierra el panel.

La sesión del dispositivo dura 12 horas; después vuelve a pedir un código. Cada
panel tiene sus propios dispositivos: emparejarse con el cierre no da acceso a
facturas.

**Certificado de la red de casa**

La conexión va cifrada con un certificado que el agente crea en tu equipo la
primera vez (`.local\red-local\ca.crt`) y que solo puede respaldar direcciones de
redes privadas, nunca un sitio de Internet. Instálalo una vez en cada dispositivo
para que el navegador deje de avisar:

1. En la página de emparejamiento, pulsa **Descargar el certificado**.
2. Comprueba que la huella que muestra el dispositivo al instalarlo coincide con
   la de la terminal.
3. Instálalo como certificado de autoridad (CA). Los menús cambian según la
   versión: en Android suele estar en Seguridad → Cifrado y credenciales →
   Instalar un certificado → Certificado de CA; en iPhone se instala como perfil y
   luego se activa en General → Información → Ajustes de confianza de
   certificados; en Windows, en «Entidades de certificación raíz de confianza» del
   usuario.

Sin instalarlo también puedes aceptar el aviso del navegador, pero entonces no hay
forma de saber si otro equipo de la red se interpone; compara siempre la huella.
No compartas `ca.key`: es la clave que firma ese certificado.

**Dispositivos emparejados**

```powershell
.\scripts\agente-local.ps1 devices                     # lista los de cada panel
.\scripts\agente-local.ps1 devices -Revoke "Celular"   # revoca uno en los dos paneles
.\scripts\agente-local.ps1 devices -RevokeAll          # revoca todos
```

La revocación aplica de inmediato, aunque el panel esté abierto. Los cambios
hechos desde otro dispositivo quedan auditados con su nombre (ver la
[auditoría del cierre](cierre-mensual.md#auditoría) y el aprobador en
[facturas](facturas.md#el-panel-de-revisión)).
