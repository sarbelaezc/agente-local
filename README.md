# Agente local

Agente local con aprobación humana obligatoria. Toda funcionalidad nueva o
modificación solicitada se declara en este README en el mismo cambio en que se
implementa.

| Feature | Estado | Módulo | Comando |
|---|---|---|---|
| Organización y clasificación de facturas electrónicas en Hotmail | En implementación; piloto supervisado con resultado mixto | `src/mail_invoices/` | `scripts/agente-local.ps1 invoices` / `-Resume` |
| Cierre mensual de gastos de Outlook con Ollama local | Lectura del buzón por Microsoft Graph, validada con importaciones reales de 2026; ingresos, pagos de tarjeta y reglas por comercio; tasa manual USD/COP y cola de avisos sin extraer; [hogar Santiago + Manuela](#hogar-santiago-y-manuela): titular por movimiento, importación de Manuela desde archivo, vista Hogar y decisión por destino (implementado, probado con datos sintéticos e importado en la base real con paridad verificada); conciliación y tarea pendientes | `src/monthly_close/` | `scripts/agente-local.ps1 <comando>` / `scripts/agente-local.ps1 import` / `scripts/agente-local.ps1 import-manuela` |

## Stack tecnológico

- **Python 3.12 o posterior**; empaquetado con `setuptools` y dependencias declaradas en `pyproject.toml`.
- **Playwright para Python** automatiza Opera para facturas y ejecuta las pruebas gráficas del panel. El cierre mensual no controla ningún navegador.
- **Microsoft Graph con `msal`** (solo lectura, permiso `Mail.Read`) lee el buzón del cierre mensual.
- **Una instancia local de Ollama compartida por facturas y cierre mensual** en `127.0.0.1:11434`; `scripts/agente-local.ps1` la inicia cuando un comando la necesita y no está activa, con el almacén de modelos predeterminado de Ollama.
- **Facturas:** `openpyxl` para Excel, `defusedxml` para XML, `pypdf` para PDF con texto, y `pypdfium2` más `Pillow` para renderizar PDF escaneados.
- **Tkinter y SQLite** de la biblioteca estándar para interfaces y persistencia local; **pytest** para pruebas.
- **Tesseract OCR** es una instalación externa opcional, necesaria solo para PDF escaneados. Opera y Ollama también se instalan aparte cuando se usa la función correspondiente.

## Reproducir en otro equipo Windows

1. Instala Git, Python 3.12 o posterior y Ollama. Instala Opera para facturas.
2. Clona el repositorio y entra en su carpeta:

    ```powershell
    git clone <URL_DEL_REPOSITORIO>
    Set-Location agente-local
    ```

3. Crea el entorno virtual, instala el proyecto y sus dependencias de desarrollo, y descarga Chromium para Playwright:

    ```powershell
    .\scripts\bootstrap.ps1
    ```

4. Crea la configuración local solo si aún no existe y completa las rutas de Opera y del libro Excel. `config/settings.yaml` está excluido de Git y debe configurarse en cada equipo, no copiarse al repositorio.

   ```powershell
   if (-not (Test-Path "config/settings.yaml")) {
     Copy-Item "config/settings.example.yaml" "config/settings.yaml"
   }
   ```

5. Descarga una sola vez el modelo compartido por ambos features, actualmente `ollama pull qwen2.5:7b`. No hace falta iniciar Ollama a mano: `scripts/agente-local.ps1` reutiliza la instancia activa en `127.0.0.1:11434` o, si no hay ninguna, la inicia y la cierra al terminar. Usa el almacén de modelos predeterminado de Ollama; si defines `OLLAMA_MODELS`, se respeta. Consulta la [guía oficial de configuración de Ollama](https://docs.ollama.com/faq#how-do-i-configure-ollama-server) para más opciones en Windows.
6. Para OCR en PDF escaneados, instala Tesseract y los datos de idioma siguiendo [Requisitos de OCR](#requisitos-de-ocr). Es opcional para las pruebas.
7. Ejecuta las pruebas sin credenciales, correos ni libros personales:

    ```powershell
    .\.venv\Scripts\python.exe -m pytest
    ```

    La configuración de pytest usa captura `sys`; no la cambies a `fd` para las pruebas gráficas de Windows.

Los comandos de facturas se muestran en sus secciones y usan `config/settings.yaml`.

## Datos y configuración locales

Todo lo que el agente guarda en el equipo vive en la carpeta `.local` del
proyecto, que Git ignora:

| Ruta | Contenido |
|---|---|
| `.local\MonthlyCloseAgent\` | `runtime.json`, `close.sqlite3` (movimientos de ambos titulares, decisiones, reglas y auditoría), copias `close.sqlite3.backup-<fecha>` creadas antes de cada migración, `graph_token_cache.bin` (sesión de Microsoft) y `last-close.txt` |
| `.local\MonthlyCloseAgent\statements.json` | Contraseña de los extractos PDF y remitentes exactos aprobados (texto plano, fuera de Git) |
| `.local\ImportManuela\` | Archivos exportados del panel de Manuela que lee `import-manuela` (movimientos, decisiones por destino y totales de paridad) |
| `.local\MailInvoiceAgent\` | Perfil de Opera, copias de seguridad del libro, `tessdata`, decisiones de clasificación y estados de lotes |
| `config\settings.yaml` | Configuración personal de facturas (también ignorada por Git) |

Las rutas relativas de `config/settings.yaml` se resuelven desde la raíz del
proyecto, sin importar desde dónde se ejecute el comando. Para guardar los datos
en otro lugar, define la variable de entorno `AGENTE_LOCAL_DATA` con esa carpeta
(sustituye a `.local`) y ajusta las rutas de `settings.yaml`. Las pruebas usan
siempre una carpeta temporal y nunca leen ni escriben `.local`.

Los estados de lotes de facturas se asocian a la ruta del perfil de Opera: si
mueves el perfil, hay que migrar esa asociación o los lotes guardados dejan de
reconocerse.

Antes estos datos estaban en `%LOCALAPPDATA%\MonthlyCloseAgent` y
`%LOCALAPPDATA%\MailInvoiceAgent`; esas carpetas ya no se usan.

## Lanzador único

`scripts/agente-local.ps1` es el único punto de entrada de ambos features.
Sustituye a `run-monthly-close.ps1`, `import-monthly-close.ps1` y
`run-invoice-batch.ps1`, que se eliminaron.

| Comando | Qué hace | Ollama |
|---|---|---|
| `serve` (predeterminado) | Panel local del cierre mensual | No |
| `login` | Inicio de sesión manual del buzón | No |
| `check-config` | Comprueba el paso 0 y la configuración | No |
| `scan` / `pilot` | Recorrido o piloto de un aviso | Sí, salvo con `-ReadOnly` |
| `import` | `check-config`, `scan` y apertura del panel | Sí |
| `summary -Month AAAA-MM [-Owner santiago\|manuela\|hogar]` | Resumen de un mes (por defecto de Santiago) | No |
| `import-manuela [-SourceDir <carpeta>]` | Importa los movimientos de Manuela desde `.local\ImportManuela` y compara sus totales | No |
| `owner-account -Owner <titular> -Suffix NNNN` | Asigna una cuenta (últimos 4 dígitos) a un titular | No |
| `reconcile-statements [-Start AAAA-MM-DD] [-ReadOnly]` | Lee por Graph los extractos PDF de Santiago, los concilia con los avisos e importa como pendientes los movimientos que solo muestra el extracto; guarda saldos de inversiones y créditos | No |
| `payroll-deduction -Owner <titular> -Suffix NNNN` | Marca un crédito cuyas cuotas se descuentan de la nómina; cada cuota que cuenta como gasto suma también como ingreso de nómina | No |
| `import-credit-ledger` | Importa los pagos de los créditos desde tu registro en Excel, tras contrastarlos al peso con los extractos | No |
| `backfill-destinations` | Vuelve a leer por Graph (solo lectura) los avisos de Santiago para guardar el destino de sus pagos ya importados | No |
| `scheduled` | Cierre de la tarea programada | Solo en la hora de cierre |
| `invoices` | Lote de facturas (`-BatchSize`, `-Days`, `-Resume`) | Sí |

Cuando un comando necesita Ollama, el lanzador reutiliza la instancia que ya
responda en el endpoint configurado y no la cierra. Si no hay ninguna, inicia
`ollama serve` en ese endpoint y al terminar o fallar cierra únicamente el
proceso que inició. En el cierre mensual comprueba además que el modelo
configurado esté instalado; nunca lo descarga. No cambia `OLLAMA_MODELS` y
restaura las variables de entorno que modifica.

Durante `scan` e `import` (sin `-ReadOnly`) la terminal muestra una barra de
progreso por carpeta según la fecha de los avisos, que se leen del más reciente
al más antiguo: `[#########...............]  36% | dinero / bancolombia |
2026-08-02 | 2 avisos`. Se llena desde el fin del rango hacia su inicio y vuelve
a empezar en cada carpeta. Solo se dibuja en una terminal interactiva; no
aparece en la tarea programada ni al redirigir la salida, y nunca muestra
contenido de los correos.

## Ejecución bajo demanda

La función de facturas no tiene servicio residente ni tarea programada. Para iniciar manualmente una
revisión de hasta 3 correos de los últimos 30 días:

```powershell
.\scripts\agente-local.ps1 invoices
```

Ajusta el tamaño (1 a 5) y el rango (1 a 365 días) cuando lo necesites; usa
`-Resume` para retomar aprobaciones persistidas sin volver a escanear:

```powershell
.\scripts\agente-local.ps1 invoices -BatchSize 5 -Days 30
.\scripts\agente-local.ps1 invoices -Resume -BatchSize 5
```

El comando requiere `.venv`, `config/settings.yaml` y Opera; inicia Ollama en el
endpoint de `invoices.ollama` si no está activo. Si falta la configuración,
crea una copia de la plantilla y se detiene para que completes los valores.
Cada ejecución abre la revisión supervisada; nada se escribe ni se mueve hasta
que selecciones y apruebes los correos en la UI.
Si la política de PowerShell bloquea los scripts `.ps1`, ejecuta el mismo modo
directamente desde la raíz del repositorio:

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config/settings.yaml --process-invoice-batch --batch-size 3 --days 30
```

## Cierre mensual de gastos: Outlook y Ollama local

### Alcance y reglas

Función con datos y flujo separados de facturas; comparte únicamente la
instancia local de Ollama con facturas. No requiere conector de Gmail. Lee el
buzón con Microsoft Graph, en solo lectura, y usa un cliente local de Ollama con
JSON Schema propio, sin mezclar el contexto ni la persistencia de los otros
features. No envía correos, mueve mensajes, los marca como leídos ni modifica
libros de facturas. Solo lee adjuntos para los extractos PDF de remitentes
aprobados, en memoria y sin guardarlos (ver [Hogar: Santiago y Manuela](#hogar-santiago-y-manuela)).

El lector por navegador (Playwright sobre Outlook Web con un perfil de Edge) se
retiró: Graph es más rápido y estable. `runtime.json` ya no admite
`"source": "browser"`, y las claves `browser_executable`, `outlook_url`,
`selectors` y `selectors_verified` que queden en un archivo antiguo se ignoran.

### Hogar: Santiago y Manuela

El panel reúne las finanzas de Santiago y de Manuela en una sola base
(`close.sqlite3`) y distingue siempre de quién es cada movimiento.

**Alcance.** Cada movimiento tiene un titular (`owner`: `santiago` o `manuela`).
Los de Manuela entran solo por archivo con `import-manuela`; no hay conexión a su
buzón, API ni navegador. El escáner de Graph y Ollama sigue leyendo únicamente el
buzón de Santiago, y todo lo que importa queda como de Santiago.

**Migración.** Al abrir una base anterior, el agente copia `close.sqlite3` a
`close.sqlite3.backup-<fecha UTC>` y añade las columnas `owner` (por defecto
`santiago`), `group_key`, `time`, `note`, `account_label`, `category_uncertain` y
`between_owners`, más las tablas `group_rules` y `owner_accounts`. No cambia
importes, decisiones ni auditoría, y registra `migrate-household` con el nombre de
la copia.

**Importación de Manuela.** Requiere el paso 0 configurado y lee
`movimientos_manuela.normalizado.json`, `decisiones_manuela.json` y, si existe,
`paridad_totales_manuela.json`:

```powershell
.\scripts\agente-local.ps1 import-manuela
```

- Valida todos los registros antes de escribir. Si uno falla (por ejemplo, un
  importe inválido o un destino sin decisión), se detiene sin importar nada e
  indica solo el número de registro, nunca su contenido.
- Escribe todo en una sola transacción. Cada movimiento recibe el identificador
  `manuela:<id>` y pasa por las mismas validaciones del panel.
- Es idempotente: los identificadores que ya existen se omiten. No toca
  `processed` ni la cobertura.
- Las compras, los ingresos y los abonos que Manuela ya contaba entran decididos
  y auditados uno a uno como `decision-manuela` («aprobado en su panel»).
- Los pagos a destinos (QR, transferencias, Bre-B, fondo, cajero, pagos
  mensuales) se deciden con su decisión por destino. Esa decisión se guarda como
  regla de destino (`group-rule-manuela`) y cada movimiento se audita como
  `decision-group-rule`. Si una persona ya cambió la regla de un destino, la
  reimportación no la sobrescribe.
- Las compras en moneda extranjera entran en COP tal como las convirtió Manuela, a
  4.000 COP/USD, con la nota «aprox.» visible en el panel.
- Las categorías dudosas se marcan como «Categoría por confirmar». Suman en
  su categoría hasta que una persona decide ese movimiento en el panel.
- Al terminar compara los totales de cada mes con `paridad_totales_manuela.json`
  y se detiene con error si algún mes no coincide.

**Reglas acordadas.**

| Tema | Regla |
|---|---|
| Retiro en cajero | Suma como gasto personal para ambos titulares (regla de Santiago). Por eso el gasto personal de Manuela queda por encima de su panel en los meses con retiros, exactamente por el monto retirado. La comparación de paridad lo descuenta y lo informa como diferencia aprobada. |
| Abonos de terceros a una tarjeta | Decisión «Abono de tercero» (`third_party_payment`): resta del gasto. Solo se permite en pagos a tarjeta. Los abonos de Manuela se importan así. En Santiago solo resta lo que se marque explícitamente; sus pagos de tarjeta existentes siguen en «No cuenta», así que sus totales no cambian. |
| «Familiar» en Hogar | Un solo rubro del hogar; el detalle muestra el titular de cada movimiento. |
| Transferencias entre Santiago y Manuela | Conservan su decisión en la vista de cada titular y no cuentan en Hogar, para no contar dos veces el mismo dinero. Se marcan solas cuando el movimiento nombra una cuenta registrada del otro titular (`*NNNN` en el comercio o una transferencia `tr:` cuyo número termina en esos dígitos), o a mano con el botón «Entre Santiago y Manuela» de la tarjeta del destino, que vale también para pagos futuros a ese destino. |
| Visibilidad | El panel es local y muestra el detalle de ambos titulares. |

Las cuentas se registran solo en la base local, nunca en Git:

```powershell
.\scripts\agente-local.ps1 owner-account -Owner santiago -Suffix NNNN
.\scripts\agente-local.ps1 owner-account -Owner manuela -Suffix NNNN
```

Registrar una cuenta revisa todos los movimientos existentes. La marca automática
nunca quita una marca puesta por una persona.

**Destino de los pagos de Santiago.** Igual que en los movimientos de Manuela,
cada pago saliente de Santiago guarda su destino (`group_key`). Lo lee un lector
determinista, sin Ollama, de la frase fija del banco; si la frase no lo dice o es
ambigua, no se asigna. Los formatos reconocidos son:

| Aviso | Destino |
|---|---|
| «Transferiste $… (por QR) desde tu cuenta … a la cuenta *NNNN» | `tr:NNNN` |
| «Transferiste $… a la llave @alias o número desde tu cuenta …» (Bre-B) | `bb:@alias` o `bb:NNNN` |
| «Pagaste $… por código QR desde tu cuenta … a la llave …» | `qr:…` |
| «Retiraste $… de tu cuenta de inversión *XXXX …» | `fd:XXXX` |

- De las cuentas y llaves numéricas se guardan solo los últimos 4 dígitos.
- Los avisos nuevos guardan el destino al importarse.
- Para los ya importados, `backfill-destinations` vuelve a leer por Graph solo los
  avisos de pagos salientes sin destino. Usa la sesión guardada, en solo lectura y
  sin Ollama, y se detiene cuando los encuentra todos.
- Solo completa destinos vacíos y audita cada uno como `destination-backfill`. No
  cambia importes, categorías ni decisiones.
- Un destino `tr:` que coincide con una cuenta registrada del otro titular se marca
  entre titulares.
- Los avisos sin estas frases (por ejemplo, aportes a inversión o cajero) siguen
  agrupados por comercio, porque no se adivinan formatos.

**Extractos de Santiago.** El banco envía cada mes extractos PDF protegidos con
contraseña. `reconcile-statements` los contrasta con los movimientos leídos de los
avisos, como Manuela contrastaba los suyos.

- **Origen.** Se buscan por Graph, en solo lectura, solo los correos de los
  remitentes exactos aprobados en `statements.json`, y solo los adjuntos con el
  nombre de extracto del banco. Un correo de otro remitente detiene la ejecución.
- **Contraseña.** Se guarda en `.local\MonthlyCloseAgent\statements.json`, en texto
  plano y fuera de Git, por decisión del usuario. Nunca se imprime.
- **Lectura.** Cada PDF se descifra y se lee en memoria; no se guarda en disco.
  Lectores deterministas, sin Ollama, para cuenta de ahorros, tarjetas (secciones
  en pesos y en dólares), Fiducuenta, crédito de consumo e hipotecario. El
  consolidado se omite porque repite Fiducuenta.
- **Validación.** El extracto de ahorros se valida con el saldo corrido, línea a
  línea. Un extracto que no se puede leer con exactitud detiene todo sin importar
  nada.
- **Doble conteo.** El mismo dinero visto en dos extractos se cuenta una vez: el
  pago de una tarjeta (débito en ahorros y abono en la tarjeta), una adición o un
  retiro del fondo, o la cuota de un crédito. También se omiten los movimientos
  que extractos consecutivos repiten.
- **Conciliación.** Un movimiento del extracto coincide con uno ya conocido del
  mismo importe y moneda, con una diferencia de hasta 5 días. Cada movimiento
  conocido se usa una sola vez. Se concilia desde `-Start` (por defecto, el 1 de
  enero del año en curso).
- **Faltantes.** Lo que solo muestra el extracto entra **pendiente**, auditado
  como `statement-import`. Ni las reglas por comercio ni las de destino lo
  deciden: siempre lo confirma una persona. Repetir el comando no importa nada
  dos veces.
- **Saldos.** Se guardan por extracto el saldo de cada inversión de Fiducuenta y el
  saldo de capital de cada crédito. El panel muestra el último saldo conocido y,
  para el mes elegido, cuántos movimientos de cada extracto coinciden.
- **Cuotas de crédito.** Cuentan como gasto cuando se deciden Personal o Familiar.
  Solo admiten Personal, Familiar o No cuenta. Antes un préstamo nunca contaba;
  como no había ninguno importado, los totales anteriores no cambian.
- **Solo lectura.** `-ReadOnly` informa la conciliación sin escribir nada.

**Frases fijas del banco sin Ollama.** Además del pago de tarjeta y la
transferencia a cuenta, se leen sin el modelo tres frases más:

| Frase del aviso | Resultado |
|---|---|
| «NOMBRE hizo un abono por $X a tu tarjeta de crédito terminada en *N» | Pago a tarjeta pendiente: «Abono de tercero» si pagó otra persona, «No cuenta» si fuiste tú; nunca ingreso |
| «transferiste $X a la llave K desde tu cuenta *N a NOMBRE» | Transferencia a NOMBRE, con la llave y la hora; nunca ingreso |
| «recibiste una transferencia de NOMBRE por $X» | Ingreso pendiente con el nombre de quien envía |

Al modelo se le indica que «transferiste» o «pagaste» nunca es ingreso, que el
comercio es el destinatario nombrado y no «Bancolombia», y cuáles son las
cuentas propias registradas (`owner-account -Owner santiago`). Una transferencia a
una cuenta propia queda como `own_transfer`.

**Créditos descontados por nómina.** Cuando la cuota de un crédito se descuenta
de la nómina, el aviso de nómina llega neto y la cuota también se cuenta como
gasto, así que el descuento restaría dos veces. Se registra el crédito una vez:

```powershell
.\scripts\agente-local.ps1 payroll-deduction -Owner santiago -Suffix NNNN
```

Desde entonces, cada cuota de ese crédito decidida Personal o Familiar crea un
ingreso «Nómina» espejo («Descuento de nómina: cuota del crédito *NNNN») por el
mismo valor y fecha, auditado como `payroll-deduction-income`. Si la cuota deja de
contar, el ingreso espejo desaparece. El gasto no cambia; el ingreso refleja el
salario completo y el balance deja de contar el descuento dos veces. Aplica a las
cuotas ya decididas y a las que lleguen con extractos futuros.

**Registro de pagos de un crédito (información complementaria).** El extracto del
crédito solo muestra el último pago de cada periodo. Cuando llevas tu propio
registro de pagos en Excel (una fila por pago, con fecha, abono a capital, pago a
capital, intereses, seguros y total), se configura en
`.local\MonthlyCloseAgent\credit_ledgers.json`, fuera de Git:

```json
[{"owner": "santiago", "suffix": "NNNN", "name": "Nombre del crédito",
  "path": "C:\\ruta\\al\\libro.xlsx", "sheet": "Hoja1",
  "bonus_months": [6, 12], "category": "Transporte"}]
```

```powershell
.\scripts\agente-local.ps1 import-credit-ledger
```

- **Contraste obligatorio.** El capital del registro entre dos cortes debe ser igual,
  al peso, a lo que bajó el saldo de capital en los extractos del banco. Si un
  periodo no cuadra, o una fila no suma su total, no se importa nada. Solo se leen
  las filas desde el mes del primer extracto disponible; las filas con total 0 (aún
  sin pagar) se omiten.
- **Reemplaza la vista parcial del extracto.** Las cuotas de ese crédito leídas del
  extracto se eliminan (con auditoría) y `reconcile-statements` deja de importarlas;
  los saldos del extracto se siguen guardando para el contraste.
- **Cada pago entra pendiente**, con capital, intereses y seguros en la nota, y se
  agrupa en el destino `cr:NNNN`. No se estima nada.
- **Origen del dinero:** las cuotas quincenales son descuento de nómina; un abono a
  capital es descuento de prima si cae en `bonus_months`, y si no, salió de tu
  cuenta. Con el crédito marcado en `payroll-deduction`, cada cuota quincenal decidida
  suma su ingreso de nómina espejo; un abono en mes de prima queda como ingreso
  pendiente para que lo confirmes; un abono propio no suma ingreso.
- **Duplicados:** un movimiento de tu cuenta con el mismo valor a menos de 3 días de
  un pago del registro queda anotado para que lo marques «No cuenta».
- **Contexto del modelo:** los créditos registrados se le pasan a Ollama como
  `known_credits`, para que un aviso de pago a esos créditos sea `loan`.

**Tasa USD sugerida.** Un movimiento USD pendiente puede llevar una tasa sugerida
(por ejemplo, el promedio de las tasas que asignaste ese mes). Queda guardada y
auditada como `exchange-suggested`, aparece en el campo de tasa del panel y solo se
usa cuando una persona decide el movimiento.

**Ingresos en la vista Año.** Muestra los ingresos del año, ingresos − gastos, una
gráfica mensual de ingresos frente a gastos y las columnas Ingresos y Balance en el
resumen por mes, para Santiago, Manuela y Hogar.

**Estilo del panel.** El panel conjunto sigue el estilo del panel de Manuela:
paleta, letra redondeada, pestañas y chips en píldora, cifras grandes, colores
fijos por categoría y calendario con intensidad por día. Usa dos columnas en
escritorio y una en celular. El tema sigue el modo claro u oscuro del sistema, y
el botón «Tema» puede fijar uno.

**Panel.** El selector **Santiago / Manuela / Hogar** (Hogar por defecto, guardado
en la URL como `owner`) filtra Mes, Año y Decisión.

- **Gasto del mes:** personal + familiar − abonos de terceros. El balance es
  ingresos − ese gasto.
- **Desglose en Hogar:** «Personal Santiago · Personal Manuela · Familiar». Cada
  movimiento lleva la etiqueta de su titular, y los marcados entre titulares
  aparecen en una cifra aparte que no cuenta en Hogar.
- **Pestaña Decisión:** agrupa los pendientes y el historial por destino (titular
  + destino; sin destino importado, el destino son las palabras del comercio).
  Cada tarjeta muestra título, número de movimientos, rango de fechas, total y una
  pista, con botones que deciden todos sus movimientos a la vez.
  - Es todo o nada: si un movimiento no pasa la validación (por ejemplo, una
    compra USD sin tasa), no cambia ninguno.
  - Cada movimiento se audita como `decision-group-local-user`.
  - «Aplicar también a pagos futuros a este destino» crea una regla de destino,
    que se puede borrar en «Reglas por destino». Sin esa casilla no se crea
    ninguna regla.
  - Debajo de cada tarjeta sigue disponible la decisión movimiento a movimiento,
    con categoría y tasa USD. Decidir un movimiento confirma su categoría.

**Seguridad y privacidad.** Los datos de Manuela viven solo en `.local\`. Las
pruebas usan datos sintéticos y nunca leen `.local`. La importación no usa red,
Graph ni Ollama, y no ejecuta nada del archivo. Toda decisión, regla y marca entre
titulares queda en `audit` con su origen.

**Verificado.**

- `pytest` con datos sintéticos cubre: migración con copia, importación validada,
  atómica e idempotente, paridad, Hogar sin doble conteo, totales de Santiago sin
  cambios, decisión y reglas por destino, abonos, el flujo del panel en Chromium y
  los comandos del lanzador.
- Una prueba en seco sobre una copia de la base real importó todos los
  movimientos sin pendientes. Cada mes coincide con el panel de Manuela, salvo la
  diferencia aprobada del cajero en los meses con retiros. Los totales y
  resúmenes de Santiago quedaron idénticos y la segunda importación no cambió
  nada.
- En la base real (con copia manual previa), `reconcile-statements` leyó todos los
  extractos de 2026 sin errores e importó como pendientes los movimientos que no
  habían llegado por aviso. Una segunda ejecución no importó nada y los totales
  decididos no cambiaron. Unos pocos importados habían quedado decididos por
  reglas por comercio; se devolvieron a pendiente con auditoría y el código ya
  lo impide.
- En la base real, `backfill-destinations` (con copia manual previa) leyó todos
  los avisos pendientes y guardó el destino de casi todos los pagos salientes de
  Santiago. Los demás no tienen una frase de destino. Importes, decisiones y totales no cambiaron. Ninguna
  transferencia fue a la cuenta registrada de Manuela.
- En la base real (con copia previa) se importó con la misma paridad, se
  registraron las cuentas de ambos titulares y los abonos de Santiago a las
  tarjetas de Manuela se marcaron entre titulares, con auditoría.

**Pendiente.**

- Las transferencias entre titulares no se detectaron automáticamente en esa
  prueba: los avisos de Santiago no guardan la cuenta destino y los datos de
  Manuela no mencionan la cuenta de Santiago. Por ahora se marcan a mano por
  destino.

### Lectura del buzón por Microsoft Graph

El agente lee el buzón por la API de Microsoft Graph con el permiso delegado
`Mail.Read`: no puede enviar, mover, borrar ni marcar mensajes. Según la
documentación de Microsoft, registrar la app y consultar correo no tiene costo
(la única API facturada de Graph es `assignSensitivityLabel` de
SharePoint/OneDrive). Ollama sigue siendo local; no se usan tokens de IA externos.

**Registro de la app (una sola vez).** Una cuenta personal sin directorio de
Microsoft Entra recibe el error `AADSTS16000` en el portal de Azure; crea antes
una cuenta gratuita de Azure, que crea tu directorio. Luego, en
portal.azure.com, Microsoft Entra ID, App registrations, New registration:

- Tipos de cuenta: debe admitir cuentas personales de Microsoft.
- Redirect URI: plataforma «Aplicaciones móviles y de escritorio» con `http://localhost`.
- API permissions: Microsoft Graph, permiso delegado `Mail.Read`.
- No crees secretos de cliente. El `Application (client) ID` no es un secreto.

**Configuración en `.local\MonthlyCloseAgent\runtime.json`:**

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

`graph_authority` es `consumers` (por defecto; app que admite cuentas personales)
o `common`. `"source": "graph"` es opcional.

**Sesión y seguridad.** `login` abre el navegador del sistema para que
inicies sesión y concedas `Mail.Read`; el agente nunca ve tu contraseña ni
controla ese navegador. Los tokens quedan en
`.local\MonthlyCloseAgent\graph_token_cache.bin`: es un dato sensible
local, fuera de Git; borrarlo cierra la sesión. `scan` y la tarea programada
nunca abren un navegador: si la sesión falta o venció, se detienen y piden
`login`. El token solo se envía a `graph.microsoft.com/v1.0`; cualquier otro
destino, incluido un enlace de paginación, detiene la ejecución.

**Cobertura.** Recorre Bandeja de entrada, Correo no deseado, Elementos
eliminados y Dinero con todas sus subcarpetas, por identificador de carpeta.
Lee cada carpeta completa, de más reciente a más antigua, en pocos segundos y la
contrasta con el total que anuncia Graph antes y después; si no coincide tras
tres intentos, se detiene e indica ambos números. Desde esa instantánea procesa
los avisos; los que lleguen durante el procesamiento se recogen en la siguiente
ejecución. El orden descendente, los identificadores repetidos, un cambio del
árbol de carpetas o superar `max_messages` también detienen el recorrido.

**Validación de cada mensaje.** Se descarga el origen MIME (máximo 2 MB, solo en
memoria) y `parse_source` exige un único `From`, `Subject`, `Message-ID` y
`Date`, que remitente y asunto coincidan con el listado, y fechas con zona
explícita; la recepción se toma del primer encabezado `Received`. Solo se
decodifica la parte de texto, o el HTML convertido a texto sin scripts, estilos
ni recursos remotos. No se decodifican ni guardan adjuntos.

**Identidad de los mensajes.** El identificador guardado es un hash de
`outlook.live.com` y el `Message-ID`. Ese origen es fijo: cambiarlo alteraría
todos los identificadores y volvería a importar avisos ya procesados.

**Estado.** Verificado con el buzón del usuario: inicio de sesión, árbol completo
de carpetas, `scan -ReadOnly` (2026-11-14: 15 carpetas, 8303 filas de metadatos,
1716 de remitentes autorizados, totales coincidentes) e importaciones reales de
2026 con Ollama. Las pruebas usan un Graph simulado: recorrido, instantánea,
cortes por fecha, errores, cliente HTTP, autenticación y un escaneo completo.

El panel local ofrece:

- **Paso 0:** las seis preguntas juntas: ubicación/zona horaria, cuentas, cuentas
  excluidas, etiqueta para transferencias, tratamiento de ahorros y transferencias
  a personas. Añade moneda, categorías, remitentes exactos y autorización para
  contenido confidencial. Las categorías se definen aquí, se guardan localmente
  y alimentan las opciones de decisión; no hay una lista predeterminada en el código.
  Como lista inicial recomendada, configura: `Domicilios y restaurantes`, `Compras`,
  `Salud y farmacia`, `Transporte`, `Suscripciones`, `Mercado y tiendas` y `Otros gastos`.
  El panel no las precarga automáticamente.
  La lectura queda bloqueada sin completar este paso.
- **Mes:** meses seleccionables y un resumen de cuatro cifras: **Gasto del mes**
  (con su reparto personal/familiar), **Ingresos**, **Balance** y **Por decidir**,
  que lleva a la pestaña Decisión. Lo que no es gasto (inversiones, pagos a
  tarjetas, retiro en efectivo) aparece debajo en una fila compacta y solo si
  hubo movimientos. Siguen el calendario, la dona por categoría (con porcentaje
  de cada una, total en el centro y colores distintos y consistentes entre
  vistas), acumulado, días de la semana, medios de pago y comercios. Las tarjetas
  de ingresos y de pagos por tarjeta se ocultan si el mes no tiene datos.
  **Detalle por categoría** separa gastos e inversiones, ingresos («confirmado»)
  y pagos de tarjeta («pagado»), e indica cuántos movimientos faltan por decidir.
- Borrar una regla o descartar un aviso de la cola pide confirmación.
- El panel permite compartir la vista seleccionada mediante la URL, incluye un
  enlace para saltar al contenido y muestra decisiones en tandas de hasta 50.
- **Año:** comparativa mensual, promedios, mes de mayor gasto, categoría líder y
  tabla personal/familiar/pendiente. Los meses no conciliados llevan advertencia.
- **Decisión:** confirmar gasto personal, familiar o no contable y corregir categoría.
  Las decisiones se guardan en SQLite y pueden revisarse después. Reimportar no
  las reemplaza. Esta vista muestra todos los meses de la moneda seleccionada,
  no solo el mes del selector.

Los importes se calculan con enteros en centésimas, no con aritmética del modelo.
No se mezclan importes de monedas distintas. Un cierre configurado en COP admite
avisos USD y su conversión estimada mediante tasa manual, conservando el original
con hasta dos decimales. Fechas ISO con zona explícita se convierten a la zona
IANA elegida. Nunca se supone UTC para una fecha sin zona.
Si el aviso no contiene fecha de compra reconocible, se usa la fecha local del
aviso y se indica en la propuesta.

Préstamos, compras rechazadas, promociones, códigos y pagos de tarjeta no cuentan
como gasto; los ingresos se confirman aparte (ver Ingresos). Las compras de tarjeta se revisan individualmente. Transferencias
propias o cuentas excluidas solo permiten Familiar o No cuenta.
**Desviación deliberada del video:** toda propuesta de gasto requiere confirmación
humana, incluso si se eligió considerar transferencias personales por defecto.
Una clasificación de Ollama no equivale a aprobación. Revisa posibles avisos
duplicados de la misma compra antes de aprobarlos.

### Compras USD y tasa estimada manual

- **Alcance:** en cierres configurados en COP, la extracción conserva USD cuando
  el aviso contiene `USD` o `US$`, en lugar de obligar al modelo a etiquetarlo COP.
  El nombre del comercio (incluido AMAZON MKTPLACE) nunca determina la moneda.
  Un aviso con marcadores COP y USD se detiene por ambigüedad, sin conversión.
- **Configuración y uso:** en **Decisión**, los movimientos USD también aparecen
  al seleccionar COP. Ingresa la **tasa estimada COP por USD para la fecha del
  movimiento**, por ejemplo `4200,50`, y confirma Personal, Familiar o la decisión
  correspondiente. Acepta punto o coma decimal, sin separadores de miles y con
  hasta seis decimales. No cuenta permite excluir el movimiento sin tasa.
  Reinicia el servidor del panel tras actualizar el código; recargar solo la
  página no actualiza el proceso Python. Si detecta un servidor anterior,
  el panel bloquea las decisiones y pide reiniciarlo.
- El importe USD permanece intacto. SQLite conserva la tasa manual y el
  equivalente COP, redondeado a centésimas con aritmética decimal. Los totales,
  gráficos mensuales/anuales y el resumen usan ese equivalente una sola vez en
  COP. Sin tasa, el importe permanece en USD y no se suma a COP.
- **Registros anteriores:** si un aviso USD quedó etiquetado como COP, comprueba
  primero el correo original, cambia **Moneda original del aviso** a USD e ingresa
  la tasa al guardar la decisión. No cambies todas las compras de Amazon por su
  nombre. Abrir el panel o reimportar no corrige ni reclasifica registros.
- **Seguridad:** solo el usuario proporciona la tasa y confirma la decisión;
  no se consulta una tasa externa ni se permite al modelo inventarla. El cambio
  de moneda/tasa se audita junto con la decisión local. Las bases anteriores
  incorporan columnas opcionales sin cambiar sus importes ni sus decisiones.
- **Validación:** pruebas sintéticas de USD explícito, rechazo de COP incorrecto,
  persistencia, migración, tasa inválida, redondeo, API y flujo del navegador.
  Pendiente: verificar avisos USD reales y las tasas elegidas por el usuario;
  no se modificaron sus registros ni se ejecutó otra importación real.

### Usabilidad y recuperación del panel

El panel conserva la aprobación humana y no cambia datos anteriores al abrirse:

- Durante una carga o guardado bloquea los controles de datos y las decisiones
  para evitar solicitudes simultáneas; el selector de tema sigue disponible.
  Si falla un guardado, muestra el error y vuelve a habilitar los controles.
- Si falla la carga, **Actualizar** permite reintentar sin recargar la página,
  incluso antes de completar la configuración inicial.
- El tema oscuro utiliza texto contrastante en los botones principales. Al cerrar
  Configuración, el foco del teclado vuelve al botón que la abrió.
- La dona tiene tamaño acotado y una leyenda para todas las categorías graficadas,
  también cuando hay más de ocho. No oculta importes por el límite de la leyenda.
  Los encabezados y las leyendas se redistribuyen en pantallas estrechas.
- El acumulado personal incluye los retiros en efectivo antiguos, igual que las
  tarjetas de totales, el calendario y el resumen anual; no modifica sus registros.

Estas mejoras no añaden dependencias ni acceso al correo, y no permiten guardar
decisiones sin pulsar explícitamente un botón de aprobación.

### Aportes a inversiones

En Decisión, el botón **Inversión** registra un aporte a un fondo u otra inversión
propia. Asigna la categoría **Inversiones** automáticamente: no necesitas añadirla
a las categorías de consumo ni reimportar un movimiento pendiente existente.
El aporte solo suma después de tu confirmación, en un total separado en Mes,
Año, la tabla mensual del año y el resumen de consola. No aumenta gastos
personales, familiares, calendario de gastos, dona, rankings ni acumulados de
consumo. Este total mide aportes menos retiros confirmados, no saldo del fondo, rendimientos,
valor de mercado ni patrimonio.

Ollama puede proponer el tipo `investment`, siempre pendiente de decisión humana.
Ese tipo permite Inversión, Retiro de inversión, Retiro en efectivo o No cuenta,
siempre mediante decisión humana, y no permite clasificarlo como consumo.
Para avisos antiguos propuestos como
compra o transferencia, también puedes elegir Inversión manualmente. El historial
conserva la decisión y permite corregirla; para volver a un gasto en un aviso
no tipado como inversión, selecciona una categoría de consumo antes de decidir.
Reimportar no sobrescribe la elección. Ingresos, préstamos, pagos de tarjeta y
cuentas excluidas no pueden sumarse a inversiones propias. No se convierte
automáticamente ninguna decisión existente ni se modifica el aporte pendiente
del usuario como parte de esta actualización.

### Decisiones manuales sobre retiros

En Decisión y en el historial se distinguen los retiros así:

- **Retiro de inversión** asigna la categoría **Retiros de inversión** y resta
  su monto de los aportes netos a inversiones del mes y del año. Puede dar un
  neto negativo si se retira dinero aportado antes del periodo. No calcula el
  saldo del fondo, rendimientos ni patrimonio y no suma gastos.
- **Retiro en efectivo** es una opción del desplegable **Categoría**, no un
  botón de decisión. Selecciónala y confirma **Gasto personal** o **Familiar**.
  Cuenta como gasto confirmado en Mes, Año, calendario, dona, acumulados y rankings.
  Su desglose en Mes, Año y tabla anual ya está incluido en el gasto, no se
  suma de nuevo. No resta inversiones.
  No registres de nuevo las compras pagadas con ese efectivo si eso duplica
  el mismo gasto.

No requieren modificar la configuración de categorías ni reimportar. Hasta
que el usuario pulse uno de los botones, el movimiento permanece pendiente sin
sumar ni restar. Se guardan en SQLite y auditoría y no se sobrescriben al
reimportar. El historial permite corregir la decisión; para volver a consumo,
elige una categoría de gasto antes de pulsar Gasto personal o Familiar.
Ingresos, préstamos, pagos de tarjeta y cuentas excluidas conservan sus
restricciones anteriores: estas opciones no sirven para saltarlas. No se
reclasifican automáticamente decisiones existentes. Las decisiones ya guardadas
como Retiro en efectivo ahora se contabilizan como gasto personal, conservando
su registro y auditoría; no hace falta reimportar.

### Ingresos

El dinero recibido (nómina, transferencias recibidas, consignaciones y otros
abonos) se extrae con el tipo `income` y queda **pendiente**: en la pestaña
Decisión se confirma con **Ingreso** o se descarta con **No cuenta** (por
ejemplo, un traslado entre cuentas propias o un reembolso). Antes estos avisos
se descartaban sin guardar nada.

- El tipo de ingreso (`Nómina`, `Transferencia recibida`, `Consignación`,
  `Otros ingresos`) se deduce de las palabras del aviso, no de Ollama, y se puede
  cambiar antes de confirmar. Un ingreso nunca puede marcarse como gasto.
- Un movimiento `transfer` o `unknown` también ofrece el botón Ingreso, por si
  una transferencia recibida llegó mal clasificada. Una compra no.
- Un retiro del propio fondo de inversión o la devolución de una compra no son
  ingresos: llegan como `unknown` para decisión humana.
- El panel muestra por mes **Ingresos** confirmados (y cuánto falta por
  confirmar), **Ingresos por tipo** y **Balance** (ingresos confirmados menos
  gastos personales y familiares). `summary` incluye `ingresos` y `balance`.
- Las reglas por comercio admiten el tipo **Ingreso**: «si quien paga contiene
  `EMPRESA`, es Nómina». Solo deciden dinero recibido.

**Recuperar meses ya importados.** Un aviso ya procesado no se vuelve a leer.
Para rescatar los ingresos descartados antes, relee solo los avisos que no
dejaron ningún movimiento:

```powershell
.\scripts\agente-local.ps1 scan -Start 2026-01-01 -End 2026-12-01 -ReprocessEmpty
```

No toca los avisos que ya tienen movimientos ni sus decisiones, y usa puntos de
reanudación propios. Como vuelve a pasar esos avisos por Ollama, también recupera
compras o transferencias que una importación anterior hubiera dejado vacías.

Verificado con pruebas del almacén, la extracción y el panel en Chromium, y en
seco con 20 avisos reales (2026-11-14): 9 ingresos detectados (2 de nómina y 7
transferencias recibidas) y, además, 8 gastos que la importación anterior había
dejado sin movimientos.

### Pagos a tarjetas de crédito

Los avisos «Pagaste [monto] en la tarjeta de credito *NNNN desde la cuenta
*NNNN, el DD/MM/AAAA» se leen con una expresión fija, **sin enviarlos a
Ollama**: monto, tarjeta y fecha se copian del aviso. Se registran solos como
`card_payment`, categoría `Pago de tarjeta`, decisión «No cuenta»: no son gasto
(el gasto son las compras hechas con la tarjeta) y no se pueden marcar como tal.
El comercio guardado es la tarjeta tal como aparece (`tarjeta de credito *8243`)
y la cuenta queda como `Desconocida`: no se adivina a qué alias corresponde.
El panel muestra por mes **Pagos a tarjetas** y **Pagos por tarjeta**. Cualquier
aviso que no tenga exactamente esa frase sigue el camino normal con Ollama.

Antes estos avisos se descartaban, y cuando el modelo los extraía podía repetir
un mismo pago en varias tarjetas. `scan -ReprocessEmpty` también vuelve a leer
los avisos cuyos únicos movimientos son esos pagos adivinados y los reemplaza
por el pago leído del aviso; queda en auditoría como
`reprocess-replace-card-payments`.

Verificado con pruebas y en seco con los avisos reales desde septiembre de 2026
(2026-11-14): 9 pagos leídos en 3 tarjetas, ninguno con esa frase sin leer.

### Evidencia literal del comercio

El comercio guardado es siempre texto copiado del aviso, nunca la redacción de
Ollama. Si el modelo cambia mayúsculas o espacios (`Cruz Verde Punto Cla` por
`CRUZ VERDE PUNTO CLA`), se recupera la forma del aviso cuando solo hay una.
En avisos sin comercio, como un retiro del fondo de inversión, si tras la única
corrección el modelo sigue describiendo la operación con sus palabras, se
conserva el fragmento más largo de esa descripción que sí aparece en el aviso
(por ejemplo, el nombre del fondo). Esto solo aplica a los tipos `unknown`,
`investment` e `income`; una compra exige el nombre completo del comercio o
queda en «Avisos sin extraer». Verificado en solo lectura con los cuatro avisos
reales que fallaban por `merchant` (2026-11-14): los cuatro producen propuesta.

**Avisos sin monto.** Algunos avisos llegan sin importe (por ejemplo, una
plantilla del banco sin rellenar: «Adicionaste {{valorAdicion}} a tu plan»). Si
el texto que el modelo propone como monto no es un importe válido, el aviso
queda en «Avisos sin extraer» con el campo `amount_text` y el recorrido
continúa; antes detenía todo el `scan`. Verificado en solo lectura con los siete
avisos reales de Inversiones que lo detenían (2026-11-14).

**Avisos informativos y cola de revisión.** Si un aviso no contiene ningún
importe marcado (`$`, `COP`, `USD`…) y el modelo no logra un monto válido, se
trata como informativo: no deja movimiento ni queda en cola. Un monto que solo
difiere en espacios (`COP $100.000,00` por `COP $ 100.000,00`) se recupera con el
texto del aviso; otro número se rechaza. El comercio también se recupera cuando
solo cambian espacios o signos (`TGALETJE` por `'T GALETJE`).

Las transferencias a un número de cuenta («Transferiste $X [por QR] desde tu
cuenta *NNNN a la cuenta *…, el fecha») se leen con una expresión fija, sin
Ollama, porque el aviso no nombra a nadie: quedan pendientes como `transfer` con
comercio `cuenta *NNNN` (solo los cuatro últimos dígitos; el número completo
nunca se guarda). Un `income` que el aviso no respalda con palabras de dinero
recibido se convierte en `unknown`.

Red de seguridad: si el aviso dice «Transferiste/Compraste/Retiraste/Enviaste»
con un importe y el modelo no devuelve ningún movimiento, el aviso queda en
«Avisos sin extraer» (campo `events`) en vez de perderse. Cada aviso de esa
lista tiene el botón **Descartar aviso**, para sacarlo cuando no trae nada que
importar; queda en auditoría y `scan -ReprocessEmpty` lo vuelve a leer.

El cliente de Graph reintenta hasta tres veces los fallos momentáneos (cortes de
red y respuestas 500, 502, 503 y 504), además del 429.

Verificado con los 15 avisos reales que estaban en cola (2026-11-15): 9
informativos sin importe, 2 inversiones y 4 movimientos recuperados (una compra
y 3 transferencias a cuenta); además se recuperaron 12 transferencias a cuenta
que importaciones anteriores habían dejado sin movimiento. La cola quedó vacía.

### Reglas por comercio

Para que el panel pregunte menos, la pestaña Decisión incluye **Reglas por
comercio**: «si el comercio contiene `PRIMAX`, es Transporte y Familiar». Las
escribe el usuario; Ollama no las crea ni las aplica. Al importar, y al guardar
una regla, las compras pendientes cuyo comercio contenga esas palabras completas
(sin distinguir mayúsculas ni signos: `UBER` coincide con `UBER*RIDES`, pero
`IPS` no coincide con `TIPS`) quedan decididas con la categoría y el tipo de la
regla. Si coinciden varias, gana la más larga.

- Solo deciden **Gasto personal** o **Familiar**, con una categoría configurada.
- Solo aplican a compras (`purchase`) pendientes. Ingresos, transferencias,
  inversiones, cuentas excluidas y compras en USD sin tasa se siguen preguntando.
- Nunca cambian una decisión ya tomada. Cada decisión automática indica su regla
  en el motivo, queda en auditoría como `decision-rule` y se puede corregir en
  «Decisiones anteriores».
- Borrar una regla no deshace las decisiones que ya tomó.

Las reglas se guardan en la tabla local `merchant_rules` de SQLite. Verificado
con pruebas del almacén, de los endpoints del panel y del formulario en Chromium.

### Configuración y primer uso en Windows

1. Actualiza el entorno con `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`.
   La dependencia `tzdata` permite zonas IANA y horario de verano en Windows.
2. Prepara la configuración técnica fuera de Git:

   ```powershell
   $data = ".local\MonthlyCloseAgent"
   New-Item -ItemType Directory -Force $data | Out-Null
   if (-not (Test-Path (Join-Path $data "runtime.json"))) {
     Copy-Item config\monthly-close.example.json (Join-Path $data "runtime.json")
   }
   .\scripts\agente-local.ps1
   ```

   Abre la dirección local indicada en consola y completa el paso 0. No
   introduzcas números de cuenta, contraseñas ni tokens. Usa alias. Puedes dejar
   remitentes vacíos mientras los identificas; eso bloquea `scan`. El panel
   escucha exclusivamente en `127.0.0.1`, no se publica en Internet.
3. Completa `graph_client_id` en `runtime.json` (ver «Lectura del buzón por
   Microsoft Graph») e inicia sesión:

   ```powershell
   .\scripts\agente-local.ps1 login
   ```

   Se abre el navegador del sistema con la página de Microsoft. El agente no
   interviene en contraseñas, MFA ni captcha.
4. **Valida el recorrido antes de importar.** `scan -ReadOnly` confirma que el
   listado de cada carpeta coincide con su total:

   ```powershell
   .\scripts\agente-local.ps1 scan -ReadOnly -Start 2026-10-01 -End 2026-11-01
   ```

   Recorre Bandeja de entrada, Correo no deseado, Elementos eliminados y
   **Dinero con todas sus subcarpetas**, identificadas por su ruta completa.
   Solo examina metadatos: no abre mensajes, no usa Ollama y no escribe
   cobertura ni decisiones. Las fechas no reducen este recorrido. Tras
   verificarlo, establece `folder_scan_verified: true` en el archivo local; la
   plantilla lo deja en `false`. Nunca se omite una carpeta que falle.
5. Aprueba en el panel las direcciones exactas de tus avisos bancarios.
6. Configura `runtime.json` con `"ollama_url": "http://127.0.0.1:11434"` y el
   mismo modelo de facturas, actualmente `qwen2.5:7b`. No inicies otra instancia
   ni descargues otra copia. `scripts/agente-local.ps1` reutiliza el Ollama
   compartido; si no está activo, lo inicia en el mismo endpoint y solo cierra
   el proceso que inició. Esto aplica a `import`, `scan`, `pilot` y a la tarea
   programada en su hora de cierre. Antes de leer el buzón verifican que el
   modelo exista; no lo descargan. La inferencia usa únicamente loopback, sin
   proxies ni herramientas para el modelo.
7. Importa de enero al mes anterior con `scan` sin fechas, o indica
   `-Start` y `-End` (fin exclusivo). Ejemplo:

   ```powershell
   .\scripts\agente-local.ps1 scan -Start 2026-01-01 -End 2026-11-01
   ```

   `max_messages` (200 en la plantilla, hasta 10000) limita las **filas en total
   entre todas las carpetas del alcance**, incluidas las de remitentes no
   autorizados; si falta otra fila, se detiene con error, sin saltarse carpetas.
   Solo se abre el origen de remitentes aprobados. Su fecha de recepción se
   convierte a la zona local: los avisos desde `End` en adelante se omiten, los
   de `Start` hasta antes de `End` se procesan y el primer aviso anterior a
   `Start` termina **esa carpeta**, no todo el recorrido. El corte se apoya en
   que Graph entrega cada carpeta en orden descendente verificado. No se envían
   a Ollama avisos fuera del rango. `-ReadOnly` mantiene el recorrido completo
   de metadatos, porque no abre orígenes para conocer sus fechas. Las carpetas
   fuera del árbol Dinero no se recorren. Si falta Dinero o cambia su árbol
   durante el recorrido, se detiene sin declarar cobertura completa.
   Los mensajes ya importados no se duplican por `Message-ID`, incluso si
   aparecen en otra carpeta. Si una ejecución falla, conserva lo ya importado
   como parcial; puedes repetirla sin sobrescribir decisiones. Pulsa Actualizar
   en el panel. Para el mes vivo, incluye sus fechas explícitamente.

### Importación masiva desde una terminal de Windows

`import` comprueba la configuración y el modelo, inicia Ollama si hace falta,
ejecuta `scan` y al terminar abre el panel. Desde la raíz del proyecto, en
**PowerShell**:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\scripts\agente-local.ps1 import -Start 2026-01-01 -End 2026-11-01
```

El cambio de política solo afecta a esa terminal y desaparece al cerrarla.
No requiere permisos de administrador ni modifica la política del equipo;
una política de organización puede impedirlo y debe respetarse.

Desde **CMD** o **Git Bash**:

```cmd
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File scripts\agente-local.ps1 import -Start 2026-01-01 -End 2026-11-01
```

El fin es exclusivo. Sin fechas importa desde enero del año local hasta el mes
anterior. Usa `-End 2026-11-03` para incluir el 2 de noviembre.

**Reanudación.** Las ejecuciones interrumpidas guardan un punto de reanudación
en la tabla local `scan_progress`: huellas SHA-256 de carpeta, identificador y
metadatos exactos, con estado pendiente o completado, sin cuerpos ni asuntos en
texto claro. Al repetir el mismo rango y configuración se verifica de nuevo todo
el árbol, pero no se vuelve a abrir el origen de avisos completados cuyos
metadatos no cambiaron. El aviso que falló queda pendiente y se reintenta al
encontrarlo. Cambiar configuración o rango inicia otro ámbito. Al terminar el
recorrido se borra su progreso. Las decisiones existentes siguen protegidas por
`Message-ID`.

**Evidencia literal y cola de revisión.** Lo que Ollama propone se valida contra
el texto del correo (ver «Evidencia literal del comercio»). Si falla la
evidencia de comercio o monto, se registra una advertencia sin contenido
financiero y se solicita **una sola corrección local** con el campo inválido y
la propuesta previa; la corrección no puede eliminar movimientos ni cambiar
otros campos. Si vuelve a fallar, el aviso no se importa ni se marca como
procesado: se guardan solo identificador, carpeta, remitente, asunto, fecha y
campo rechazado en la cola visible en **Decisión**, y el recorrido continúa con
los demás avisos. El panel avisa de que faltan movimientos en los totales y no
permite marcar ese mes como revisado. Repite el rango para reintentar los
pendientes, o descártalos desde el panel si no traen nada que importar. Los
fallos de sesión, de Graph y las demás validaciones sí detienen la ejecución.

Dos movimientos indistinguibles en un mismo correo detienen el escaneo sin
importar ese aviso; no se eliminan duplicados por código.

**Panel al terminar.** Incluso si el escaneo devuelve un error, `import` inicia
el panel en otra terminal y abre `http://127.0.0.1:8766` en el navegador
predeterminado; si ya está activo, lo reutiliza. Espera su respuesta antes de
abrir la página y rechaza una página distinta en ese puerto. Mantén la terminal
del panel abierta mientras revisas. Se conserva el código de error de la
importación: abrir el panel no significa que el recorrido terminara bien. Con
`-ReadOnly` no abre el panel ni importa. El script restaura las variables de
entorno que modificó y no instala tareas.

Las pruebas del lanzador usan un servicio simulado y cubren la reutilización de
Ollama, los argumentos, el código de salida, el modelo ausente y la apertura del
panel (existente, iniciado por el script o puerto ocupado por otra página).

### Cobertura, cierre y tarea persistente

Cada mes importado se marca **parcial/sin conciliar**, aunque no haya avisos. Nunca
se certifica completitud contando correos. Se recorren Entrada, Spam, Eliminados y
Dinero con todos sus descendientes directamente; no se afirma cobertura de otras carpetas ni de mensajes ya borrados
por retención. Compara los totales con
extractos: el sistema no tiene una API bancaria
ni puede saber cuánto cobró una tarjeta sin datos adicionales.
La revisión manual del panel conserva una nota de conciliación. Movimientos
nuevos invalidan esa revisión; repetir la misma importación no borra decisiones.

Después de configurar y validar el buzón, instala opcionalmente la tarea:

```powershell
.\scripts\register-monthly-close.ps1 -WhatIf
.\scripts\register-monthly-close.ps1
Get-ScheduledTaskInfo -TaskName "AgenteLocal-MonthlyClose"
```

Usa el **Programador de tareas de Windows**, no un cron de sesión ni herramientas
de Claude. Persiste al cerrar VS Code. Una comprobación horaria solo inicia
lectura **el último día del mes, durante las 22:00 de la zona configurada**.
Python aplica las reglas de zona/horario de verano; las demás ejecuciones no
leen el buzón. Requiere Windows con sesión interactiva abierta, equipo
encendido y Ollama instalado (el lanzador lo inicia en la hora de cierre si no está activo). Si la sesión de Microsoft venció, falla
sin continuar y pide `login`. No instala servicios ni tareas sin ejecutar este script.

Cada ejecución carga desde disco las respuestas, reglas, categorías y remitentes.
No depende de memoria de chat ni de enlaces publicados. SQLite conserva el
histórico y las decisiones; el panel refleja el mes nuevo sin republicación.
El resumen queda en `last-close.txt` dentro del directorio privado de datos.
También se consulta con:

```powershell
.\scripts\agente-local.ps1 summary -Month 2026-10
.\scripts\register-monthly-close.ps1 -Remove
```

El resumen indica gasto confirmado, familiar y dudas, no totales del modelo sin
aprobar. Un cierre a las 22:00 no incluye compras posteriores: vuelve a importar
ese mes al día siguiente antes de conciliarlo. No hay envío de notificaciones
por correo ni garantía de ejecución si el equipo está apagado.

### Privacidad, límites y validación

- `scan` lista remitente, asunto, fecha e identificador de todos los mensajes de
  las carpetas del alcance para verificar el recorrido. Solo descarga el origen
  MIME de remitentes aprobados; los demás no se abren ni se mandan a Ollama. No
  se transmiten mensajes a servicios de IA externos.
- El agente se comunica con Microsoft Graph, en solo lectura. El modelo no
  dispone de terminal, disco, red ni herramientas; sus respuestas JSON se
  validan y jamás se ejecutan.
- No se guardan cuerpos en SQLite ni en logs. Se retienen los hechos financieros,
  decisiones y auditoría local hasta borrado manual.
- Para borrar todos los datos de esta función: detén panel, lecturas y tarea;
  elimina manualmente `close.sqlite3` del directorio de datos (se pierden
  decisiones e histórico) y `graph_token_cache.bin` para cerrar la sesión de
  Microsoft. No afecta a facturas. No se realiza borrado automático del histórico.
- No hay cifrado propio ni autenticación multiusuario. Usa una cuenta Windows
  protegida y cifrado de disco si procede. Otros procesos del mismo equipo pueden
  acceder al panel local. Si el proyecto está en una carpeta sincronizada o
  compartida (OneDrive, Drive…), `.local` se sincroniza con ella: define
  `AGENTE_LOCAL_DATA` con una ruta fuera de esa carpeta si no quieres que tus
  datos y sesiones salgan del equipo.

Pruebas reproducibles sin credenciales ni correo personal:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\monthly_close
```

Se validan reglas, importes, zona horaria, decisiones persistentes, exclusión de
remitentes no autorizados, validación MIME, el lector de Graph con un buzón
simulado y la protección del panel. Las pruebas gráficas del panel usan Chromium
instalado por `bootstrap.ps1`: formulario inicial, cambios de configuración,
totales con centavos, decisiones, etiquetas de los gráficos, reintento tras
fallos de carga, bloqueo de guardados duplicados y contraste mínimo de 4,5:1 en
ambos temas.
**Pendiente de validar con el usuario:** conciliación contra extractos y
ejecución de la tarea instalada.

### Piloto de un solo aviso

El comando `pilot` valida **como máximo un aviso de un remitente autorizado**,
sin recorrer el histórico ni afirmar cobertura de carpetas. Requiere el paso 0
completo, sesión iniciada y `runtime.json`. Toma el aviso autorizado más
reciente entre los últimos mensajes de cada carpeta del alcance; no busca ni
abre mensajes fuera de esa lista.

```powershell
.\scripts\agente-local.ps1 pilot -ReadOnly
.\scripts\agente-local.ps1 pilot
```

La primera variante verifica lectura e identidad en memoria, sin Ollama ni
importación. La segunda comprueba primero que el endpoint compartido de Ollama
tiene el modelo configurado y luego propone movimientos para revisión humana.
Repetirla no duplica el mismo `Message-ID` ni reemplaza decisiones.

En equipos que bloqueen scripts de PowerShell puede usarse la entrada Python
equivalente, sin modificar la política de ejecución (requiere Ollama ya activo
para los comandos que lo usan):

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m monthly_close.main serve
.\.venv\Scripts\python.exe -m monthly_close.main pilot --read-only
.\.venv\Scripts\python.exe -m monthly_close.main scan --read-only --start 2026-10-01 --end 2026-11-01
```

## Asistentes de código: GitHub Copilot y Claude Code

Todas las reglas del proyecto están en un solo archivo, [`AGENTS.md`](AGENTS.md):

| Archivo | Quién lo lee | Contenido |
|---|---|---|
| [`AGENTS.md`](AGENTS.md) | Copilot directamente; Claude Code a través de `CLAUDE.md` | Reglas del proyecto, seguridad y catálogo/selección de skills |
| [`CLAUDE.md`](CLAUDE.md) | Claude Code | Una sola línea, `@AGENTS.md`, porque Claude Code no lee `AGENTS.md` por sí solo |
| [`.claude/skills/`](.claude/skills/) | Ambos | Única ubicación de las skills compartidas |
| [`.claude/settings.json`](.claude/settings.json) | Claude Code | Permisos compartidos: impide leer el token de Microsoft y el perfil de Opera |

Para cambiar una regla, edita solo `AGENTS.md`. Los ajustes
personales de Claude Code van en `.claude/settings.local.json`, que Git ignora.

El catálogo y los criterios para elegir la skill adecuada se mantienen solo en
[`AGENTS.md`](AGENTS.md). Las instrucciones completas de cada skill están en su
`SKILL.md` dentro de [`.claude/skills/`](.claude/skills/).

## Aviso

Proyecto personal y experimental, sin relación con Microsoft ni con Opera.
Automatiza interfaces web de terceros: cada usuario es responsable de cumplir los
términos de servicio de esos productos y las políticas de su organización.
Nunca subas al repositorio tus archivos de configuración personales, Excel,
facturas, perfiles de navegador ni bases de datos de auditoría.

## Hotmail: nuevo alcance en implementación

### Organización del código

El código ejecutable vive en `src/` y las pruebas en `tests/`.
Las pruebas se agrupan por feature: `tests/mail_invoices/` para facturas de
Hotmail y `tests/monthly_close/` para cierre mensual. Esta organización no cambia
el comportamiento de las funciones.
Ejecuta todas con `.\.venv\Scripts\python.exe -m pytest`, o solo una feature con
`.\.venv\Scripts\python.exe -m pytest tests\monthly_close` (sustituye la carpeta
para probar otra feature).
`mail_invoices` agrupa la funcionalidad de facturas:

- `main.py`: argumentos y selección del modo de ejecución.
- `workflow.py`: modos de solo lectura y extracción compartida de adjuntos.
- `review.py`: datos y reglas de revisión, sin depender de Tkinter.
- `review_ui.py`: ventana, selección y aprobación humana.
- `browser.py` e `inbox.py`: interacción con Opera y Outlook.
- `documents.py`, `ocr.py`, `assist.py` y `local_model.py`: extracción y asistencia local.
- `workbook.py`: validación, duplicados, respaldo y escritura verificada del Excel.
- `settings.py`: lectura de la sección de facturas de la configuración unificada.
- `classification_decisions.py`: decisiones confirmadas y extractos de contexto en SQLite local.
- `batch_state.py`, `batch_review_ui.py` y `batch_workflow.py`: estados persistentes,
  revisión conjunta y ejecución recuperable de lotes pequeños.

Los controles de aprobación se prueban sin abrir ventanas. Una prueba gráfica
separada construye Tkinter y acciona los botones en un proceso aislado con un
libro sintético; no requiere correo ni Excel real. Las pruebas de la CLI verifican
sus argumentos y las del flujo conservan los errores informados.

Pytest usa captura `sys` de stdout y stderr. En este entorno Windows, la captura
`fd` reproduce fallos intermitentes al inicializar Tcl/Tk después de leer un XLSX,
incluso fuera del código del agente. No fuerce `--capture=fd` para estas pruebas
gráficas. Una regresión repite la lectura de XLSX y la apertura de Tkinter en el
mismo proceso. Este ajuste corrige el modo de ejecución de las pruebas, no modifica
la instalación de Python ni incorpora reintentos que oculten errores de la aplicación.

Se preparó un módulo de facturas, con extracción
local de XML UBL, ZIP y PDF con texto. La lectura de XML sigue siendo la fuente
preferida para los datos estructurados de la factura. Dentro de un ZIP, un PDF
se trata como la representación gráfica de un XML (estructura DIAN) y no se
analiza ni pasa por OCR u Ollama solo si se empareja de forma uno a uno e
inequívoca con ese XML: mismo nombre de archivo sin extensión, o el número de
factura del XML como palabra del nombre del PDF. Así un PDF ilegible no bloquea
la factura XML válida. Cualquier PDF sin pareja inequívoca se sigue analizando,
aunque el ZIP traiga tantos PDF como XML. Si un archivo del ZIP falla y otro es
válido, se conservan las facturas válidas y el fallo se informa con la ruta de
ese archivo (`adjunto.zip:archivo.pdf`). Ese error sigue bloqueando el correo en
el lote hasta revisarlo, porque el archivo ilegible podría ser otra factura. Si
ningún archivo del ZIP es válido, falla todo el adjunto como antes. Probado con ZIP sintéticos; falta
confirmar los nombres que usan los proveedores reales.

### Ollama local y OCR

Facturas usa `qwen2.5:7b`. Se evaluó `qwen3.5:9b` y se descartó: con datos
sintéticos y el modelo 100 % en GPU dio las mismas respuestas, pero tardó 76,7 s
en completar campos y 104,2 s en clasificar, frente a 7,4 s y 2,8 s con Qwen 2.5.
El retraso procede de su razonamiento previo a la respuesta.

Implementado y probado con datos sintéticos (aún no con facturas ni correos reales):

- **OCR local de PDF escaneados** (`ocr.py`): si un PDF no tiene texto, sus
  páginas se renderizan en memoria con `pypdfium2` y se leen con Tesseract
  (`spa+eng`). Límites: 5 páginas, tamaño de página y texto acotados, tiempo
  máximo por página. Sin Tesseract, el PDF escaneado queda para revisión manual.
- **Completado de campos con Ollama** (`assist.py`, `local_model.py`): para PDF
  con texto u OCR, el modelo local propone solo los campos que las reglas no
  encontraron. Cada valor se acepta únicamente si aparece en el texto del
  documento (número, razón social, fecha, total y moneda COP); lo que no se
  puede ubicar se descarta y se informa como rechazado.
- **Sugerencia de carpetas** (`folders.py`): en este orden, reglas confirmadas en
  `folder_rules` (remitente exacto o `@dominio`), luego historial unánime del
  mismo remitente o dominio y, por último, Ollama con la lista de carpetas
  existentes y ejemplos mínimos (remitente, asunto y carpeta, sin cuerpos de
  correo). Este orden se conserva para consultas de metadatos solamente.
  En la clasificación de facturas se prioriza el contenido: proveedor, conceptos
  y partidas del XML o texto del PDF/OCR, seguido del asunto y cuerpo del correo.
  Las reglas por remitente y el historial por dominio no imponen el destino.
  La sugerencia debe ser una carpeta existente; nunca se crean carpetas.

Reglas de seguridad de esta ampliación:

- Ollama solo se acepta en `127.0.0.1`, `localhost` o `::1`, sin proxy ni
  herramientas; el texto del documento se trata como dato no confiable.
- El XML y las reglas deterministas prevalecen; el modelo nunca reemplaza un
  valor existente. Toda factura que no venga completa de XML, o con campos
  propuestos por el modelo, queda marcada `requires_review`.
- Ollama no registra datos, no mueve correos ni cambia carpetas. La interfaz exige
  aprobación explícita antes de registrar un lote en Excel; un correo solo se moverá
  después de verificar el registro o confirmar que es duplicado.
- No se inventan valores ni se descargan enlaces externos.

La simulación de clasificación obtiene las carpetas reales y permite revisar
destinos. El lector de historial requiere autorización por carpeta. Se revisaron localmente
63 carpetas y se guardaron 291 ejemplos de remitente/asunto, excluyendo una fila
sin dirección verificable; esos ejemplos no incluyen cuerpos o facturas históricas
ni se cargan automáticamente. Son contexto, no entrenamiento de pesos. El movimiento
se habilita solo con aprobación en lotes supervisados pequeños, descritos abajo.

### Simular clasificación de facturas (sin mover)

```powershell
  .\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --classify-invoices --days 30 --candidate-text NUMERO_FACTURA
```

`--candidate-text` filtra el texto de la lista/preview antes de abrir mensajes;
úsalo con un identificador de factura para revisar un único candidato. Abrirlo
puede marcarlo como leído. Corregir y guardar su destino solo registra un ejemplo
local para futuras sugerencias; esta simulación nunca mueve correos.

Lee las carpetas existentes de Outlook, abre hasta `invoices.batch_limit` correos
candidatos y descarga sus adjuntos en subdirectorios `classification-N` de
`invoices.attachments_dir`. Abrir un correo puede marcarlo como leído. Verifica
número, proveedor, fecha y valor contra el Excel configurado **sin escribirlo**.
Una coincidencia histórica sin número no equivale a registro confirmado.
Tras abrir cada candidato, lee exclusivamente su cuerpo identificado en Outlook,
no los textos de otras filas. El análisis con Ollama se hace localmente, sin proxy:
hasta 6000 caracteres de cuerpo y hasta 10 facturas, con proveedor y extractos
acotados de conceptos/texto (1000 caracteres por campo y factura). Los textos se
tratan como datos no confiables, nunca como instrucciones. Más de 10 facturas
requiere revisión manual. Si se omite Ollama, no se sustituye este análisis por
una decisión basada solo en el remitente. No se persisten cuerpos en el informe;
los adjuntos se eliminan al terminar cada ejecución.
Solo se borra el subdirectorio temporal creado por esa ejecución; otros archivos
de `attachments_dir` y las fuentes de `--analyze-file` no se modifican.
Los errores y las facturas pendientes bloquean la aptitud para un movimiento
posterior. La ventana muestra el estado de cada factura, el destino propuesto
y el motivo; puede cambiar la carpeta o dejarla vacía. **Cerrar la simulación
no mueve correos**. El informe se imprime en JSON sin los extractos de contexto.
Una sugerencia de Ollama no es una aprobación.

#### Guardar correcciones y reutilizarlas

1. Elija la carpeta correcta en cada desplegable.
2. Marque **«Confirmo esta carpeta como ejemplo para futuras sugerencias»**
   solo en los correos que haya revisado; las casillas empiezan desmarcadas.
3. Verifique **«Confirmado por»**, precargado con `invoices.approver`.
4. Pulse **«Guardar decisiones confirmadas»**. Guardar no mueve correos ni
   registra facturas; el destino puede confirmarse aunque siga pendiente en Excel.
   Una selección sin carpeta o sin nombre de confirmador se rechaza.

Las decisiones se guardan en
`.local\MailInvoiceAgent\classification-decisions.sqlite3`, ignorado por Git y fuera del
repositorio, con carpeta, confirmador y fecha UTC. Se retienen remitente (120
caracteres), asunto y proveedor (200), un extracto del cuerpo (1000) y hasta
10 facturas con proveedor (200), conceptos y texto (500 por campo).
No se guardan adjuntos, rutas de archivos ni importes en esta base.
Contiene datos personales: no la sincronice ni la suba a Git. Se conserva hasta
que la elimine manualmente, con el agente cerrado, para reiniciar este contexto.

Las próximas simulaciones cargan estas decisiones automáticamente y envían a
Ollama hasta 6 ejemplos relacionados por contenido (presupuesto de 6000 caracteres
de ejemplos, extractos más cortos que los guardados), excluyendo carpetas que
ya no existen. Las decisiones de otros correos se conservan; volver a confirmar
el mismo contexto sustituye su carpeta anterior. Son ejemplos para el contexto,
no entrenamiento de pesos ni reglas automáticas por remitente. Las sugerencias
siguen requiriendo revisión humana; `--no-model` no usa estos ejemplos para decidir.
Cerrar la ventana sin guardar no confirma nada. Los errores de almacenamiento
se muestran y no se notifican como éxito.

El historial de correos archivados no se lee por defecto. Autorice carpetas
con su ruta exacta (solo remitente y asunto de hasta 5 filas visibles, sin abrir
mensajes), por ejemplo:

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --classify-invoices --history-folder "Compras/Recibos"
```

`--no-model` omite Ollama. La prueba real sin historial encontró las carpetas
y facturas pendientes en el Excel original; por ello no se efectuó ningún
movimiento. La simulación nunca mueve; el movimiento real requiere un lote
supervisado y la aprobación explícita de su destino.

### Lotes pequeños con revisión unificada y estados locales

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --process-invoice-batch --batch-size 3 --days 30
```

Prepara hasta 3 correos por defecto (entre 1 y 5 con `--batch-size`, independiente
de `invoices.batch_limit`, que solo usa `--classify-invoices`). Cada ejecución abre
Opera una sola vez y reutiliza esa sesión para clasificar, revisar y archivar;
`--resume-invoice-batch` sin pendientes termina sin abrir Opera. La ventana muestra las facturas
editables, la carpeta sugerida y los errores de cada correo. La selección es por
correo, empieza desmarcada e incluye TODAS sus facturas; un adjunto con errores
bloquea la selección. Puede usar «Seleccionar correos aptos» y desmarcar después.
Revise cada destino: una carpeta propuesta por Ollama no es una autorización.
La ventana permite desplazarse con la rueda del mouse. Cada factura ofrece
«Abrir factura (PDF)» cuando existe un PDF directo o un único PDF asociado en el
ZIP; el botón queda deshabilitado si no hay una asociación inequívoca. Los PDFs
se abren desde el directorio temporal del lote, que se elimina al cerrar.
Los campos rechazados por Ollama se muestran como advertencias editables. Corrija
los valores con el PDF y revise TODAS las facturas del correo; solo se puede
aprobar el correo completo y todos sus campos deben validar. Los errores de
extracción o identidad siguen bloqueando el correo.
Una diferencia entre el número del asunto y el XML/PDF se muestra como advertencia
corregible, no como aprobación automática: contraste el documento y corrija el
campo antes de aprobar el correo completo.
La columna `Empresa` se sugiere a partir del historial del Excel cuando todas las
filas con la misma razón social usan la misma empresa; si no hay una coincidencia
histórica inequívoca, usa la razón social del documento. Verifique la sugerencia.
El campo «Aprobado por» se precarga con `approver` y sigue siendo editable.

**«Aprobar y ejecutar lote»** guarda las aprobaciones y autoriza, para cada correo
seleccionado, registro en Excel con respaldo, guardado local de la carpeta como
ejemplo y movimiento al destino elegido, sin otra ventana de aprobación.
Los posibles duplicados históricos conservan sus confirmaciones. Si falta un
campo, carpeta o confirmador, no se guarda la aprobación del lote. Cerrar sin
ejecutar conserva los pendientes, pero no escribe Excel ni mueve correos.

Los estados viven en `.local\MailInvoiceAgent\invoice-batches.sqlite3`,
separados por perfil, libro y hoja: `pending`, `approved`, `registered`, `moving`,
`moved` y `blocked`. Se retienen identificador, metadatos, campos necesarios para
registrar las facturas, extractos acotados de contexto, destino, confirmador,
fecha de aprobación y último error. No se guardan adjuntos ni evidencia completa
del PDF en esa base. Es información personal local: no la sincronice ni la suba
a Git. Los registros se conservan hasta su eliminación manual; **no elimine la
base para resolver un movimiento incierto**, porque perdería las autorizaciones
y el estado de recuperación. Los adjuntos descargados solo existen durante la
ejecución y se eliminan al cerrar, cancelar o fallar el flujo.

Para retomar sin descargar ni clasificar de nuevo:

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --resume-invoice-batch --batch-size 3
```

La ventana muestra los pendientes y las aprobaciones previas. Pulsar ejecutar
retoma estas últimas sin volver a pedir sus datos o carpeta. Un estado `approved`
registra de forma idempotente y verifica Excel; `registered` verifica el libro
y continúa solo el archivo. Antes del clic de mover se guarda `moving`. Si el
proceso se interrumpe ahí, la siguiente ejecución **solo comprueba la carpeta
aprobada y nunca vuelve a mover automáticamente**. Si no puede verificarla,
permanece pendiente de revisión con un error explícito. `moved` no se procesa
de nuevo. Un conflicto de registro bloquea el archivo. Si el Excel está abierto
al ejecutar, el correo conserva su estado `approved` con el error y se reintenta
tras cerrar el libro; no pasa a `blocked` ni exige volver a aprobarlo.
Los correos `blocked` muestran campos editables precargados con la última revisión.
Corrija el conflicto en Excel o los datos de la ventana y seleccione explícitamente
el correo para volver a aprobarlo. Cerrar no altera su estado ni su autorización.
Una nueva aprobación revalida todos los campos y el registro antes de mover;
no modifica silenciosamente filas existentes. Los movimientos `moving` siguen
siendo de verificación solamente, sin esta opción de reaprobar el movimiento.

El lote se detiene ante el primer fallo y conserva las demás aprobaciones.
Un bloqueo del sistema operativo impide ejecutar dos de estos lotes a la vez
y se libera al cerrar o terminar el proceso. Excel y Outlook siguen sin ser
una transacción atómica. La verificación de identidad incluye remitente, asunto
y, antes de mover, una huella del cuerpo leído; un cambio bloquea el movimiento.
Si el cuerpo cambia después de registrar, el mensaje pasa a `blocked` y no se
reintenta el movimiento automáticamente. Las demás aprobaciones quedan guardadas;
revise manualmente ese correo antes de volver a aprobarlo. El agente no reemplaza
el digest aprobado con el cuerpo nuevo ni mueve el correo usando una aprobación
obsoleta.
La ejecución programada y la aprobación automática sin supervisión siguen
pendientes. Un piloto real supervisado terminó con cuatro correos movidos y
verificados y uno bloqueado porque su cuerpo cambió después de la revisión. El
candidato con discrepancia entre asunto y PDF pudo corregirse en la UI; la factura
se validó con su PDF. El piloto sigue teniendo resultado mixto y no habilita
ejecución autónoma. El correo bloqueado requiere reconciliación manual antes de
otra aprobación. Las pruebas cubren
aprobación conjunta, persistencia, cancelación, recuperación sin duplicar registros,
bloqueo de ejecuciones simultáneas y una página sintética con los selectores de
movimiento. Esto no valida la precisión general del modelo ni convierte el piloto
mixto en una validación exitosa.

### Escanear la bandeja (solo lectura)

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --scan-inbox --days 30
```

Lee la lista de la bandeja de Hotmail y muestra los correos con adjunto cuyo asunto
o vista previa mencionan factura, invoice, recibo, CFDI o comprobante. Usa `--days`
para la ventana (1 a 365, por defecto 30). No abre, descarga ni mueve correos. Los
correos sin fecha reconocible se muestran para revisión.
La búsqueda de un candidato recorre la lista virtualizada hasta que el scroll
deja de avanzar, con un límite de 60 páginas. No interpreta un número constante
de filas visibles como final de la bandeja.

### Analizar un adjunto local (solo lectura)

Extrae los datos de un XML, PDF o ZIP guardado en el equipo y los muestra en
JSON, con el método usado (`xml`, `pdf_text`, `ocr`), los campos completados
por el modelo y los rechazados. No escribe en el Excel ni toca el correo.

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --analyze-file C:\ruta\factura.pdf
```

Usa `--no-model` para ver solo el resultado de reglas y OCR, sin Ollama.

### Modos retirados

`--process-one-invoice`, `--download-attachments` y `--review-invoices` se
eliminaron. El lote supervisado (`--process-invoice-batch`, con `--batch-size 1`
para un solo correo) cubre descarga, revisión, registro y archivo, y además usa
el bloqueo de ejecución única y los estados recuperables. Una prueba real previa
con `--process-one-invoice` registró y movió un correo verificado; ese resultado
no se ha repetido con el lote de un correo. Para consultas de solo lectura siguen
`--scan-inbox`, `--classify-invoices` y `--analyze-file`.

### Requisitos de OCR

1. `winget install --id UB-Mannheim.TesseractOCR -e`
2. Descarga `spa.traineddata` de
   [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) a
   `.local\MailInvoiceAgent\tessdata` y copia ahí `eng.traineddata`
   desde `C:\Program Files\Tesseract-OCR\tessdata`.
3. Ajusta la sección `invoices.ocr` de `config/settings.yaml` si usas otras rutas.

La instancia Ollama de facturas es compartida con cierre mensual: su endpoint en
`invoices.ollama` debe coincidir con `ollama_url` de
`.local\MonthlyCloseAgent\runtime.json`, y ambos usan el mismo tag de
modelo.

El registro preparado utiliza `Hoja1`, encabezados en B2:E2 y añade
`Número de factura` en F2 solo al guardar un lote aprobado. Conserva las filas
existentes, comprueba números por proveedor y crea una copia de seguridad local
antes de reemplazar el Excel. Una coincidencia histórica sin número requiere
confirmación; no se completa por inferencia. No se mezclan monedas: el esquema
actual admite solo valores confirmados en COP.
Un duplicado por número y proveedor también debe coincidir con la `Empresa`
aprobada, además de fecha e importe. Una diferencia se informa como conflicto,
sin sobrescribir el libro ni archivar el correo. Esto también se verifica antes
de mover y al asociar el número a una fila histórica.
Las nuevas celdas de `Valor` conservan el formato numérico de la fila de datos
anterior de esa columna (incluido el símbolo monetario y los decimales); se
omiten filas sin importe. Sin datos previos se usa `#,##0.00`. El importe sigue
siendo numérico, no texto. Una restauración explícita mediante
`record(..., restore_amount_format=True)` solo admite facturas existentes con
número, proveedor, fecha e importe coincidentes; crea respaldo y verifica los
valores y el formato antes de reemplazar el libro. No agrega filas.

Las pruebas del escritor usan únicamente libros sintéticos en directorios temporales;
nunca escriben en el Excel real. La interfaz solicita aprobación explícita antes de
llamar al registro. La simulación de carpetas ya se probó con Outlook real;
los lotes pequeños exigen aprobación para el movimiento real,
como se indica arriba.

La configuración personal está en `config/settings.yaml`, excluida de Git.
La sección `invoices` contiene Hotmail, Excel (incluidos `ocr` y `approver`) y
el endpoint compartido de Ollama para facturas y cierre mensual. Una sección
`teams` heredada de versiones anteriores se ignora y puede borrarse.
El lector conserva compatibilidad con archivos planos anteriores mediante
`--config`; para migrarlos, agrupe sus ajustes en `invoices` y conserve todos los
valores personales. En configuraciones antiguas, el bloque global `ollama` se
conserva como endpoint compartido de facturas y cierre mensual.
No copie la plantilla encima de una configuración personal existente.
La plantilla es `config/settings.example.yaml`. El perfil de Opera y las
copias locales están bajo `.local\MailInvoiceAgent`, fuera de los
perfiles habituales de Opera.

Inicio de sesión manual (sin escanear ni mover mensajes):

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --login --config config\settings.yaml
```

Opera se comprobó con Playwright en una página local. Esto no valida todavía
su sesión ni sus selectores en Outlook. Completa el acceso manual y cierra la
ventana; no compartas credenciales ni copies cookies.

## Licencia

MIT. Ver [LICENSE](LICENSE).
