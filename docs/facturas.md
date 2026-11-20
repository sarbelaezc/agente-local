# Facturas electrónicas de Hotmail

Encuentra en la Bandeja de entrada de Hotmail los correos con facturas, extrae sus
datos, te los muestra en un panel local para que los revises y, solo con tu
aprobación, registra cada factura en tu Excel y mueve el correo a su carpeta.

Para instalar y configurar, ver
[configuracion.md](configuracion.md#7-facturas-settingsyaml).

- [Cómo funciona](#cómo-funciona)
- Tareas:
  - [Procesar un lote](#procesar-un-lote)
  - [Retomar un lote](#retomar-un-lote)
  - [Corregir un correo bloqueado](#corregir-un-correo-bloqueado)
  - [Limpiar lotes](#limpiar-lotes)
  - [Consultas de solo lectura](#consultas-de-solo-lectura)
- Referencia:
  - [El panel de revisión](#el-panel-de-revisión)
  - [Extracción de las facturas](#extracción-de-las-facturas)
  - [Sugerencia de carpetas](#sugerencia-de-carpetas)
  - [Estados de un lote](#estados-de-un-lote)
  - [El libro de Excel](#el-libro-de-excel)
  - [Acceso al buzón](#acceso-al-buzón)
  - [Datos que se guardan](#datos-que-se-guardan)
- [Estado](#estado)

## Cómo funciona

1. **Candidatos.** Por Microsoft Graph lista los correos de la Bandeja de entrada
   con adjuntos, de los últimos días indicados, cuyo remitente, asunto o vista
   previa mencionan factura, invoice, recibo, CFDI o comprobante. Listarlos no
   abre ningún correo. Los de `excluded_senders` se ignoran.
2. **Extracción.** Descarga los adjuntos PDF, XML y ZIP en una carpeta temporal y
   extrae cada factura: primero del XML de la DIAN; si no hay, del texto del PDF o
   con OCR; Ollama completa solo los campos que faltan y cada valor que propone
   debe aparecer en el documento.
3. **Carpeta.** Ollama propone la carpeta destino a partir del contenido de la
   factura y de los ejemplos que ya confirmaste.
4. **Revisión.** El panel muestra cada correo con sus facturas editables y la
   carpeta propuesta. Tú eliges qué correos aprobar.
5. **Ejecución.** Para cada correo aprobado: registra sus facturas en el Excel
   (con copia de seguridad), verifica el registro y solo entonces mueve el correo
   y comprueba que llegó a la carpeta.

Una sugerencia de Ollama nunca es una aprobación. No hay ejecución programada ni
aprobación automática: cada lote necesita a una persona en el panel.

## Procesar un lote

```powershell
.\scripts\agente-local.ps1 invoices                         # 10 correos de los últimos 90 días
.\scripts\agente-local.ps1 invoices -BatchSize 20 -Days 180 # hasta 20 correos, 1 a 365 días
```

1. El lanzador inicia Ollama si hace falta, prepara el lote y abre el panel en el
   navegador. Si falta `config\settings.yaml`, lo crea desde la plantilla y se
   detiene para que lo completes. Si la sesión venció, se detiene y pide
   `login -Mailbox facturas`.
2. En el panel, revisa cada correo: corrige los campos con el PDF («Ver PDF») y
   confirma la carpeta. La casilla de cada correo empieza vacía y aprueba **todas**
   sus facturas; «Seleccionar correos aptos» marca los que no tienen errores.
3. Pulsa **«Aprobar y ejecutar»**. El panel muestra el avance de cada correo
   (registrado, archivado o con error) y el resultado.

«Cerrar sin ejecutar», o cerrar la pestaña, termina sin escribir el Excel ni mover
correos; los pendientes se conservan para la próxima vez. El lote se detiene en el
primer fallo y conserva las demás aprobaciones. No se pueden ejecutar dos lotes a
la vez.

Ollama tarda unos 3 a 10 segundos por correo; el límite de 20 es lo que conviene
revisar de una vez. Sin PowerShell:
`.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --process-invoice-batch --batch-size 10 --days 90`.

## Retomar un lote

```powershell
.\scripts\agente-local.ps1 invoices -Resume
```

Abre el panel con los pendientes y las aprobaciones anteriores, sin volver a
descargar ni clasificar. Si no hay nada pendiente, termina sin conectarse al buzón.
Pulsar ejecutar continúa lo aprobado sin volver a pedir sus datos:

- Un correo **aprobado** se registra (sin duplicar filas) y se verifica en el Excel.
- Uno **registrado** verifica el libro y continúa con el archivo.
- Uno que quedó **moviéndose** cuando se cortó el proceso solo se comprueba en la
  carpeta aprobada; nunca se vuelve a mover automáticamente. Si no se puede
  verificar, queda pendiente con el error.
- Si el Excel estaba abierto, el correo sigue aprobado con el error y se reintenta
  al cerrarlo, sin volver a aprobarlo.

No borres la base de estados para resolver un movimiento dudoso: perderías las
autorizaciones y el estado de recuperación.

## Corregir un correo bloqueado

Un correo pasa a **bloqueado** cuando su registro choca con el Excel (por ejemplo,
el número ya existe con otra fecha o valor) o cuando su cuerpo cambió después de
aprobarlo.

1. Ábrelo con `invoices -Resume`: aparece con los campos de la última revisión.
2. Corrige el conflicto en el Excel o en los datos del panel.
3. Selecciónalo explícitamente y apruébalo de nuevo. La nueva aprobación vuelve a
   validar todo antes de registrar y mover; nunca modifica en silencio filas
   existentes.

## Limpiar lotes

```powershell
.\scripts\agente-local.ps1 invoices-cleanup                    # vista previa, no borra nada
.\scripts\agente-local.ps1 invoices-cleanup -Apply             # borra lo que mostró la vista previa
.\scripts\agente-local.ps1 invoices-cleanup -OlderThanDays 90  # otra retención (0 a 3650 días)
```

Borra de la base de estados (`invoice-batches.sqlite3`) solo esto:

| Grupo | Se borra |
|---|---|
| Correos archivados de la configuración actual | Si se archivaron hace más de `-OlderThanDays` días (30 por defecto; 0 borra todos). El Excel y el buzón ya tienen el resultado |
| Pendientes de un remitente de `excluded_senders` | Siempre: el panel ya no los muestra |
| Registros de otra configuración (otra app, libro u hoja) | Todos: el agente nunca vuelve a usarlos |

- Nunca borra un correo aprobado, registrado, moviéndose o bloqueado de la
  configuración actual, ni un pendiente normal. No toca el Excel, el buzón ni las
  decisiones de carpetas.
- Con `-Apply`, primero copia la base a
  `invoice-batches.sqlite3.backup-AAAAMMDD-HHMMSS`, borra exactamente lo previsto
  (si no coincide, no aplica nada) y compacta el archivo. La copia conserva todo:
  bórrala cuando compruebes el resultado.
- El informe solo tiene cantidades y queda en el registro de la ejecución con quién
  la aprobó y cuándo. No corre mientras haya un lote en curso.
- Si un correo archivado vuelve a la bandeja, se clasifica como nuevo; al
  aprobarlo, el Excel reconoce la factura ya registrada y no la agrega dos veces.

## Consultas de solo lectura

Estos modos no escriben el Excel ni mueven correos. Se ejecutan con Python:

```powershell
# Lista los candidatos de la bandeja, sin abrirlos
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --scan-inbox --days 30

# Extrae los datos de un archivo guardado en el equipo (XML, PDF o ZIP)
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --analyze-file C:\ruta\factura.pdf

# Simula la clasificación de carpetas y compara con el Excel, sin escribirlo
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --classify-invoices --days 30 --candidate-text NUMERO_FACTURA
```

- `--analyze-file` muestra el método usado (`xml`, `pdf_text`, `ocr`), los campos
  completados por el modelo y los rechazados. `--no-model` omite Ollama.
- `--classify-invoices` abre hasta `invoices.batch_limit` candidatos (o solo los
  que contienen `--candidate-text` en remitente, asunto o vista previa) y muestra
  en el panel el destino propuesto y su motivo. Puedes corregir la carpeta y, con
  la casilla «Guardar esta carpeta como ejemplo», guardarla para futuras
  sugerencias (con «Confirmado por»). Guardar no mueve ni registra nada.
- `--history-folder "Compras/Recibos"` (con `--classify-invoices`) autoriza leer
  remitente y asunto de hasta 5 correos de esa carpeta como contexto, sin abrirlos.

## El panel de revisión

Se abre en el navegador en `http://127.0.0.1:<puerto libre>/` y se cierra al
terminar, con el mismo estilo del panel del cierre mensual y tema claro u oscuro.
Con `invoices -Red` también se puede revisar y aprobar desde tus dispositivos
emparejados de la red de casa, en `https://<dirección del equipo>:8768/` (ver
[Paneles desde otros dispositivos](configuracion.md#11-paneles-desde-otros-dispositivos-opcional)).
Una aprobación desde otro dispositivo guarda el aprobador con el dispositivo, por
ejemplo «Santi (desde Celular)». El lote sigue terminando si ningún dispositivo
consulta el panel durante 90 segundos.

- **Cada correo es una tarjeta** con sus avisos, la carpeta destino y sus facturas
  editables: empresa, razón social, fecha, valor, número y moneda. El estado solo
  aparece cuando deja de ser «Pendiente de aprobación» (aprobado, archivado,
  bloqueado…). Un correo con errores de extracción se muestra en solo lectura, con
  cada dato junto a su nombre.
- **Carpeta sugerida.** Bajo «Carpeta destino» se indica qué carpeta se sugiere y
  por qué, por ejemplo «Sugerida: Salud (historial del proveedor)».
- **Valor.** Debajo del valor se muestra cómo quedará registrado, por ejemplo «Se
  registrará: $ 119.000 COP», con la misma lectura de separadores que usa el
  agente al escribir el Excel: `1.234` es mil doscientos treinta y cuatro y
  `1.234,5` lleva decimales. Si el valor es ambiguo, lo dice antes de aprobar.
- **Cabecera y barra.** La cabecera muestra solo el nombre del Excel (la ruta
  completa aparece al pasar el cursor). «Seleccionar correos aptos» está encima de
  la lista; la barra fija de abajo deja «Aprobado por», «Cerrar sin ejecutar» y
  «Aprobar y ejecutar», en una sola fila de botones en el móvil.
- **Advertencias corregibles.** Los campos que Ollama no pudo respaldar y una
  diferencia entre el número del asunto y el del documento se muestran como
  advertencias: contrasta con el PDF y corrige antes de aprobar.
- **Empresa.** Se sugiere del historial del Excel cuando todas las filas con esa
  razón social usan la misma empresa; si no, se usa la razón social. «Aprobado
  por» se precarga con `approver`.
- **Validación.** Cada campo se valida al salir de él; al aprobar, un resumen de
  errores enlaza a cada campo. El servidor vuelve a validar todo y guarda todas las
  aprobaciones juntas antes de escribir o mover; si algo falla, no guarda ninguna.
- **Duplicados históricos.** Si una factura coincide con una fila del Excel sin
  número, debes decidir: asociar el número a esa fila o agregarla como nueva.
- **Cierre.** Cerrar la pestaña con cambios sin aprobar o durante la ejecución pide
  confirmación. Si la pestaña se cierra, el panel deja de esperar a los 90
  segundos; si no se abre en 10 minutos, no se ejecuta nada.
- **Seguridad.** Solo escucha en `127.0.0.1`, exige Host y Origin exactos y un
  token por ejecución, y no carga recursos externos. Los PDF se sirven por un
  token del lote, nunca por ruta. Todo texto de un correo se muestra como texto,
  nunca como HTML.

## Extracción de las facturas

- **XML primero.** El XML (UBL de la DIAN) es la fuente preferida. Un PDF que es la
  representación gráfica de un XML (mismo nombre sin extensión, o el número de la
  factura en el nombre del PDF, emparejados uno a uno) no se analiza, dentro de un
  ZIP o suelto en el correo. Un PDF sin pareja inequívoca se analiza siempre.
- **Contenedores sin factura.** Algunos proveedores envían un `AttachedDocument`
  de la DIAN que solo trae la respuesta de validación. Se omite sin bloquear el
  correo y la factura se lee del PDF que lo acompaña, marcada para revisar contra
  el PDF. Un contenedor sin ningún otro documento es un error.
- **ZIP con fallos.** Si un archivo del ZIP falla y otro es válido, se conservan
  las facturas válidas y el fallo se informa (`adjunto.zip:archivo.pdf`); el correo
  sigue bloqueado hasta revisarlo, porque ese archivo podría ser otra factura.
- **PDF escaneados.** Si un PDF no tiene texto, se leen hasta 5 páginas con
  Tesseract (`spa+eng`), con límites de tamaño y tiempo. Sin Tesseract, queda para
  revisión manual.
- **Ollama.** Propone solo los campos que las reglas no encontraron (número, razón
  social, fecha, total y moneda COP) y cada valor se acepta solo si aparece en el
  texto del documento. Nunca reemplaza un valor existente. Toda factura que no
  venga completa del XML queda marcada para revisión.
- Un correo con más de 10 facturas requiere revisión manual. No se descargan
  enlaces externos ni se inventan valores.

## Sugerencia de carpetas

1. **Proveedor ya confirmado.** Si todas tus decisiones confirmadas con ese
   proveedor (comparado sin tildes, mayúsculas ni espacios de más) fueron a la
   misma carpeta, y esa carpeta existe, se sugiere esa sin consultar a Ollama:
   «Sugerida: Compras/Falabella (tus decisiones anteriores con …)». Funciona
   también con `--no-model`. Si el proveedor es nuevo o lo archivaste en
   carpetas distintas, decide el paso siguiente.
2. **Ollama**, a partir del contenido: proveedor, conceptos y texto de hasta 10
   facturas, el asunto y el cuerpo del correo, la lista de carpetas existentes y
   hasta 6 ejemplos confirmados relacionados por contenido. Sin Ollama no hay
   sugerencia para estos correos y eliges la carpeta tú.

`folder_rules` y el historial por remitente solo se usan cuando no hay contenido
(solo metadatos); nunca imponen el destino de una factura. La sugerencia siempre
es una carpeta existente; nunca se crean carpetas. Las decisiones confirmadas (en
el lote o con `--classify-invoices`) son ejemplos y antecedentes por proveedor,
no entrenamiento del modelo; siempre puedes cambiar la carpeta antes de aprobar.

## Estados de un lote

| Estado | Significado |
|---|---|
| `pending` | Preparado, sin aprobar |
| `approved` | Aprobado; falta registrar en el Excel |
| `registered` | Registrado y verificado; falta mover el correo |
| `moving` | Se pidió el movimiento; si se cortó, solo se verifica |
| `moved` | Archivado y verificado; no se procesa de nuevo |
| `blocked` | Conflicto con el Excel o cuerpo cambiado; requiere nueva aprobación |

La identidad de cada correo incluye remitente, asunto y una huella SHA-256 del
cuerpo leído; un cambio antes de mover lo bloquea, y el agente nunca mueve con una
aprobación obsoleta. El Excel y el buzón no forman una transacción atómica: por
eso existen estos estados y la verificación de cada paso.

## El libro de Excel

- Se usa la hoja `Hoja1` con encabezados en B2:E2; la columna `Número de factura`
  (F2) se agrega al guardar el primer lote aprobado. Las filas existentes se
  conservan.
- Antes de reemplazar el libro se crea una copia en `backup_dir`; tras verificar
  la escritura se conservan las 5 copias más recientes.
- Una factura ya registrada (mismo proveedor y número) no se agrega de nuevo, pero
  su fecha, valor y empresa deben coincidir; una diferencia es un conflicto, sin
  sobrescribir el libro ni archivar el correo.
- Una coincidencia histórica sin número requiere tu confirmación; nunca se
  completa por inferencia.
- Solo se admiten valores en COP. La celda de valor es numérica y copia el formato
  de la fila anterior (o `#,##0.00`).

## Acceso al buzón

La app de Microsoft tiene `Mail.ReadWrite`, pero el cliente (`graph_mailbox.py`)
solo envía peticiones `GET` bajo `/v1.0/me/` y una única escritura:
`POST /v1.0/me/messages/{id}/move`. Borrar, enviar, copiar, marcar como leído o
crear carpetas se rechaza antes de salir del equipo. El token solo se envía a
`graph.microsoft.com/v1.0`.

- **Cuerpo:** en texto plano, hasta 6000 caracteres.
- **Adjuntos:** solo PDF, XML y ZIP de hasta 10 MB, con nombres saneados. Los
  enlaces a la nube y los correos adjuntos no se descargan.
- **Carpetas:** el árbol completo por ruta («Padre/Hija»), sin las del sistema.
- **Mover:** con identificadores que no cambian al moverse; después se verifica
  que el correo está en la carpeta aprobada con el mismo remitente y asunto. Una
  respuesta perdida no se repite. Un rechazo por exceso de peticiones (429) sí se
  reintenta, porque Graph no actuó.

## Datos que se guardan

| Dónde | Qué |
|---|---|
| `.local\MailInvoiceAgent\invoice-batches.sqlite3` | Estados de lotes: identificador, metadatos, campos para registrar, extractos acotados de contexto, destino, aprobador, fecha y último error. Sin adjuntos ni el PDF |
| `.local\MailInvoiceAgent\classification-decisions.sqlite3` | Carpetas confirmadas como ejemplo: remitente, asunto, proveedor, extractos acotados del cuerpo y de hasta 10 facturas, carpeta, confirmador y fecha. Sin adjuntos ni importes |
| `.local\MailInvoiceAgent\graph_token_cache.bin` | Sesión de Microsoft de facturas |
| `backup_dir` | Copias del Excel (las 5 más recientes) |
| `.local\MailInvoiceAgent\logs\` | Registros de ejecución (los 10 más recientes) |

Los adjuntos solo existen durante la ejecución y se borran al terminar, cancelar
o fallar. No se guardan cuerpos completos. Las dos bases contienen datos
personales: no las subas a Git.

## Estado

**Verificado con datos reales**

- Lotes completos supervisados en el buzón real: inicio de sesión, lectura,
  revisión en el panel, registro en el Excel y movimiento verificado.
- Simulación de carpetas con el buzón real por Graph.
- Registro de ejecuciones y limpieza de lotes en la base real (2026-11-18).
- En los 25 correos procesados hasta noviembre de 2026 todas las facturas llegaron
  en ZIP.
- Sugerencia de carpeta (2026-11-20), en solo lectura con tus 32 decisiones
  confirmadas, ocultando cada una: 24 correctas (75 %) con el proveedor primero,
  frente a 21 (66 %) solo con Ollama. Las 19 resueltas por proveedor fueron todas
  correctas; los errores restantes son proveedores nuevos o archivados en
  carpetas distintas, sobre todo entre subcarpetas de Compras. Restringir la
  respuesta de Ollama a la lista de carpetas no cambió el resultado (el modelo
  nunca propuso una carpeta inexistente) y no se aplicó.

**Cubierto por pruebas** (`pytest tests\mail_invoices`): Graph simulado (lista de
operaciones permitidas, reintentos, carpetas, adjuntos, huella del cuerpo,
movimiento y verificación), aprobación conjunta, persistencia, cancelación,
recuperación sin duplicar registros, bloqueo de lotes simultáneos, el panel en
Chromium (incluido un contraste mínimo de 4,5:1 en ambos temas y que la vista
previa del valor lee los montos igual que el escritor del Excel) y el escritor de
Excel con libros sintéticos.

**Pendiente**

- Los nombres de archivo que usan los proveedores reales para emparejar XML y PDF
  sueltos (probado con archivos sintéticos).
- No consta una prueba con un PDF escaneado real (OCR).
- Las pruebas no miden la precisión general del modelo: cada campo se revisa en el
  panel.
- Los cambios de presentación del panel (vista previa del valor, carpeta sugerida,
  barra compacta en el móvil) no se han usado todavía con un lote real.
- Aprobar un lote real desde un celular con `invoices -Red` (probado con
  pruebas HTTPS sobre una segunda dirección local).
