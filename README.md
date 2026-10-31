# Agente local

Agente local con aprobación humana obligatoria. Toda funcionalidad nueva o
modificación solicitada se declara en este README en el mismo cambio en que se
implementa.

| Feature | Estado | Módulo | Comando |
|---|---|---|---|
| Organización y clasificación de facturas electrónicas en Hotmail | En implementación; validación controlada y sintética | `src/mail_invoices/` | `python -m mail_invoices.main --classify-invoices` / `--process-invoice-batch` / `--resume-invoice-batch` |
| Respuestas en Teams | WIP pausado | `src/app/`, `src/automation/`, `src/ai/`, `src/storage/` | `python -m app.main` |

## Stack tecnológico

- **Python 3.12 o posterior**; empaquetado con `setuptools` y dependencias declaradas en `pyproject.toml`.
- **Playwright para Python** automatiza los navegadores. Facturas usa Opera; Teams usa un perfil separado de Microsoft Edge y permanece pausado.
- **Dos instancias locales de Ollama**, una exclusiva por feature; `httpx` hace las llamadas y `PyYAML` gestiona la configuración. Teams usa `127.0.0.1:11435`; facturas usa `127.0.0.1:11434`.
- **Facturas:** `openpyxl` para Excel, `defusedxml` para XML, `pypdf` para PDF con texto, y `pypdfium2` más `Pillow` para renderizar PDF escaneados.
- **Tkinter y SQLite** de la biblioteca estándar para interfaces y persistencia local; **pytest** para pruebas.
- **Tesseract OCR** es una instalación externa opcional, necesaria solo para PDF escaneados. Opera, Edge y Ollama también se instalan aparte cuando se usa la función correspondiente.

## Reproducir en otro equipo Windows

1. Instala Git, Python 3.12 o posterior y Ollama. Instala Opera para facturas; Edge solo si vas a preparar Teams. Teams está pausado y no debe ejecutarse.
2. Clona el repositorio y entra en su carpeta:

    ```powershell
    git clone <URL_DEL_REPOSITORIO>
    Set-Location agente-local
    ```

3. Crea el entorno virtual, instala el proyecto y sus dependencias de desarrollo, y descarga Chromium para Playwright:

    ```powershell
    .\scripts\bootstrap.ps1
    ```

4. Crea la configuración local solo si aún no existe y completa las rutas de Opera y del libro Excel. La plantilla deja `teams.allowed_chats` vacío; no autoriza conversaciones. `config/settings.yaml` está excluido de Git y debe configurarse en cada equipo, no copiarse al repositorio.

   ```powershell
   if (-not (Test-Path "config/settings.yaml")) {
     Copy-Item "config/settings.example.yaml" "config/settings.yaml"
   }
   ```

5. Inicia cada instancia de Ollama en una terminal PowerShell independiente, con puerto y directorio de modelos propios. Cierra la instancia automática de Ollama antes de levantar servicios manuales para evitar que ocupe el puerto `11434`.

    Facturas:

    ```powershell
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    $env:OLLAMA_MODELS = "$env:LOCALAPPDATA\MailInvoiceAgent\OllamaModels"
    ollama serve
    ```

    Teams (solo cuando se reactive):

    ```powershell
    $env:OLLAMA_HOST = "127.0.0.1:11435"
    $env:OLLAMA_MODELS = "$env:LOCALAPPDATA\TeamsLocalAgent\OllamaModels"
    ollama serve
    ```

    En otra terminal, define el mismo `OLLAMA_HOST` y ejecuta `ollama pull <modelo>` para descargar el modelo configurado en la sección correspondiente. Los modelos y sus datos permanecen separados. Consulta la [guía oficial de configuración de Ollama](https://docs.ollama.com/faq#how-do-i-configure-ollama-server) para más opciones en Windows.
6. Para OCR en PDF escaneados, instala Tesseract y los datos de idioma siguiendo [Requisitos de OCR](#requisitos-de-ocr). Es opcional para las pruebas.
7. Ejecuta las pruebas sin credenciales, correos ni libros personales:

    ```powershell
    .\.venv\Scripts\python.exe -m pytest
    ```

    La configuración de pytest usa captura `sys`; no la cambies a `fd` para las pruebas gráficas de Windows.

`scripts/run.ps1` inicia el agente de Teams, no el organizador de facturas. No lo ejecutes mientras Teams siga pausado. Los comandos de facturas se muestran en sus secciones y usan `config/settings.yaml`.

## Skill de revisión: Ponytail

Instalada para este proyecto en
[`.github/skills/ponytail/SKILL.md`](.github/skills/ponytail/SKILL.md).
Fuente: [dietrichgebert/ponytail](https://github.com/dietrichgebert/ponytail),
revisión `96608e078b2d5ea607c5c3b8b5a54d1c1fba6846`, con su
[licencia MIT](.github/skills/ponytail/LICENSE). Solo se instalaron las
instrucciones de la skill y su licencia, sin scripts, hooks ni plugins externos.

Puede solicitar «usa Ponytail para revisar el proyecto» o invocar `/ponytail`
cuando el cliente la detecte. Su enfoque es el cambio más simple que resuelva
completamente el problema, sin recortar validación, seguridad o pruebas.
La instalación no envía código ni datos personales a Ponytail.
La lista de skills de una sesión abierta puede requerir una nueva sesión para
reconocerla; en la sesión de instalación se leyeron sus instrucciones directamente.

## Aviso

Proyecto personal y experimental, sin relación con Microsoft ni con Opera.
Automatiza interfaces web de terceros: cada usuario es responsable de cumplir los
términos de servicio de esos productos y las políticas de su organización.
Nunca subas al repositorio tus archivos de configuración personales, Excel,
facturas, perfiles de navegador ni bases de datos de auditoría.

**Teams: WIP pausado.** No ejecutar el agente de Teams mientras este alcance esté
pausado. La autenticación al reabrir Playwright y los selectores reales siguen sin
validarse. La nueva prioridad es la organización de facturas de Hotmail en un Excel
existente; hay un núcleo probado, pero el flujo completo aún no está disponible.

## Hotmail: nuevo alcance en implementación

### Organización del código

El código ejecutable vive en `src/` y las pruebas en `tests/`.
`mail_invoices` agrupa la funcionalidad de facturas:

- `main.py`: argumentos y selección del modo de ejecución; conserva los comandos.
- `workflow.py`: orquesta sesión, descarga, extracción y revisión.
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

Teams continúa separado en los paquetes `app`, `automation`, `ai`, `storage` y
`config`; permanece pausado. Esta refactorización no cambia sus comandos ni lo activa.

Se preparó un módulo de facturas independiente del de Teams, con extracción
local de XML UBL, ZIP y PDF con texto. La lectura de XML sigue siendo la fuente
preferida para los datos estructurados de la factura.

### Ollama local y OCR

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
se habilita solo con aprobación en el modo integrado de un correo o en lotes
supervisados pequeños, descritos abajo.

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
`%LOCALAPPDATA%\MailInvoiceAgent\classification-decisions.sqlite3`, fuera del
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
movimiento. La simulación nunca mueve; el movimiento real requiere el modo
integrado y la aprobación explícita de su destino.

### Prueba integrada de UN correo (escribe Excel y mueve con aprobación)

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --process-one-invoice --days 30
```

Procesa únicamente el primer candidato, independientemente de `batch_limit`:
descarga y extrae las facturas, abre la revisión habitual y registra solo las
seleccionadas al pulsar «Aprobar y guardar lote». El escritor crea el respaldo
del Excel. Deben quedar registradas TODAS las facturas del correo y no existir
errores de extracción para habilitar el movimiento. Cancelar o aprobar solo
parte del correo bloquea su archivo, sin deshacer registros ya aprobados.

Después permite revisar y guardar ejemplos de carpeta. Esto no mueve nada.
Una ventana separada **«Aprobar movimiento de UN correo»** exige seleccionar
un destino existente y pulsar **«Aprobar y mover este correo»**. Cancelar deja
el correo en la bandeja y conserva el registro de Excel. Se vuelve a comprobar
el registro después de esta aprobación y se valida la identidad estable del
correo antes de mover. Se verifica que aparece en la carpeta aprobada.

Si falla la verificación tras intentar mover, el informe indica
`movement_status: unverified`; no se reintenta automáticamente. Revise Outlook
antes de repetir la prueba: Excel y Outlook no forman una transacción atómica.
No crea carpetas y no procesa otros correos. Ollama sigue proponiendo, no
autorizando destinos; sus sugerencias pueden ser incorrectas.

Se verificó una prueba con un correo real: registro aprobado en el Excel
original, creación de respaldo, corrección y guardado local del destino,
aprobación del movimiento y presencia del mismo correo en la carpeta elegida.
Una sesión nueva confirmó el registro y el destino sin escribir en Excel.
La primera tentativa se bloqueó antes de mover por la carga asíncrona del árbol;
se añadió la espera y se reanudó solo el movimiento aprobado, sin repetir la
escritura. En esa etapa la suite pasó 138 pruebas. Esto valida una prueba controlada, no
clasificación masiva ni precisión general del modelo.

### Lotes pequeños con revisión unificada y estados locales

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --process-invoice-batch --batch-size 3 --days 30
```

Prepara hasta 3 correos por defecto (entre 1 y 5 con `--batch-size`, independiente
del `batch_limit` de los modos anteriores). La ventana muestra las facturas
editables, la carpeta sugerida y los errores de cada correo. La selección es por
correo, empieza desmarcada e incluye TODAS sus facturas; un adjunto con errores
bloquea la selección. Puede usar «Seleccionar correos aptos» y desmarcar después.
Revise cada destino: una carpeta propuesta por Ollama no es una autorización.

**«Aprobar y ejecutar lote»** guarda las aprobaciones y autoriza, para cada correo
seleccionado, registro en Excel con respaldo, guardado local de la carpeta como
ejemplo y movimiento al destino elegido, sin otra ventana de aprobación.
Los posibles duplicados históricos conservan sus confirmaciones. Si falta un
campo, carpeta o confirmador, no se guarda la aprobación del lote. Cerrar sin
ejecutar conserva los pendientes, pero no escribe Excel ni mueve correos.

Los estados viven en `%LOCALAPPDATA%\MailInvoiceAgent\invoice-batches.sqlite3`,
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
de nuevo. Un conflicto de registro bloquea el archivo.
Los correos `blocked` muestran campos editables precargados con la última revisión.
Corrija el conflicto en Excel o los datos de la ventana y seleccione explícitamente
el correo para volver a aprobarlo. Cerrar no altera su estado ni su autorización.
Una nueva aprobación revalida todos los campos y el registro antes de mover;
no modifica silenciosamente filas existentes. Los movimientos `moving` siguen
siendo de verificación solamente, sin esta opción de reaprobar el movimiento.

El lote se detiene ante el primer fallo y conserva las demás aprobaciones.
Un bloqueo del sistema operativo impide ejecutar dos de estos lotes a la vez
y se libera al cerrar o terminar el proceso. No ejecute simultáneamente los
modos antiguos: no participan de este bloqueo. Excel y Outlook siguen sin ser
una transacción atómica. La verificación de identidad incluye remitente, asunto
y, antes de mover, una huella del cuerpo leído; un cambio bloquea el movimiento.
La ejecución programada y la aprobación automática sin supervisión siguen
pendientes. La suite actual pasa 195 pruebas, incluidas aprobación conjunta,
persistencia, cancelación, recuperación sin duplicar registros, bloqueo de
ejecuciones simultáneas y una página sintética con los selectores de movimiento.
Este modo no se ha probado todavía con un lote real de varios correos.

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

### Descargar y analizar adjuntos

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --download-attachments --days 30
```

Escanea la bandeja igual que `--scan-inbox`, abre cada candidato (máximo `batch_limit`),
guarda sus adjuntos PDF, XML y ZIP en un subdirectorio temporal de `attachments_dir`
(por defecto `tmp\`) y los analiza en solo lectura. El subdirectorio se elimina al
terminar. Omite archivos mayores a 10 MB y no escribe en el Excel. Ten en cuenta
que **abrir un correo lo marca como leído** en Outlook. Usa `--no-model` para no llamar a
Ollama. Los candidatos que fallan se listan en el JSON con `requires_review`.

### Revisar y registrar un lote

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --review-invoices --days 30
```

Descarga y analiza los adjuntos como el modo anterior, y luego abre una ventana de
revisión. Las facturas empiezan sin seleccionar; puede marcarlas individualmente o
usar «Seleccionar todas» y desmarcar las que no autoriza. Seleccionar no escribe.
La columna `Empresa` se
sugiere a partir del historial del Excel cuando todas las filas con la misma razón
social usan la misma empresa; si no hay una coincidencia histórica inequívoca, usa
como sugerencia la razón social del documento. Verifique la sugerencia antes de
aprobar. El campo "Aprobado por" se precarga con `approver` de la configuración y
sigue siendo editable. La ventana muestra los campos
extraídos y cualquier campo descartado por Ollama. La ruta de destino y el aviso de
respaldo están visibles antes de guardar. Pulsar «Aprobar y guardar lote» es la
autorización explícita para escribir, sin un diálogo final adicional. Cerrar sin
guardar no modifica el Excel. Las coincidencias históricas conservan sus diálogos
de confirmación explícita. El registro crea
una copia de seguridad, verifica el archivo temporal y no mueve correos. Ollama se usa
por defecto para completar campos de PDF; `--no-model` lo omite y deja esos campos
para completar a mano.

### Analizar un adjunto local (solo lectura)

Extrae los datos de un XML, PDF o ZIP guardado en el equipo y los muestra en
JSON, con el método usado (`xml`, `pdf_text`, `ocr`), los campos completados
por el modelo y los rechazados. No escribe en el Excel ni toca el correo.

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\settings.yaml --analyze-file C:\ruta\factura.pdf
```

Usa `--no-model` para ver solo el resultado de reglas y OCR, sin Ollama.

### Requisitos de OCR

1. `winget install --id UB-Mannheim.TesseractOCR -e`
2. Descarga `spa.traineddata` de
   [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) a
   `%LOCALAPPDATA%\MailInvoiceAgent\tessdata` y copia ahí `eng.traineddata`
   desde `C:\Program Files\Tesseract-OCR\tessdata`.
3. Ajusta la sección `invoices.ocr` de `config/settings.yaml` si usas otras rutas.

La instancia Ollama de facturas debe estar activa en `invoices.ollama`; Teams
usa de forma independiente `teams.ollama`. Cada sección define su URL y modelo.

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
el modo de un correo y los lotes pequeños exigen aprobación para el movimiento real,
como se indica arriba.

La configuración personal está en `config/settings.yaml`, excluida de Git.
Ambos módulos usan este archivo: `teams` contiene los ajustes de Teams y su
instancia `ollama`; `invoices` contiene Hotmail, Excel (incluidos `ocr` y
`approver`) y su propia instancia `ollama`. Las URLs deben ser distintas. La
unificación no activa Teams, que sigue pausado.
Los lectores conservan compatibilidad con archivos planos anteriores mediante
`--config`; para migrarlos, agrupe sus ajustes en las secciones indicadas,
conserve todos los valores personales y configure una instancia Ollama separada
para cada feature. En configuraciones antiguas, el bloque global `ollama` se
conserva como instancia de facturas; Teams usa su endpoint nuevo por defecto.
No copie la plantilla encima de una configuración personal existente.
La plantilla es `config/settings.example.yaml`. El perfil de Opera y las
copias locales están bajo `%LOCALAPPDATA%\MailInvoiceAgent`, fuera de los
perfiles habituales de Opera y Teams.

Inicio de sesión manual (sin escanear ni mover mensajes):

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --login --config config\settings.yaml
```

Opera se comprobó con Playwright en una página local. Esto no valida todavía
su sesión ni sus selectores en Outlook. Completa el acceso manual y cierra la
ventana; no compartas credenciales ni copies cookies.

## Teams: alcance pausado

Aplicacion local que prepara borradores de Teams Web y exige aprobacion humana antes de insertar o enviar texto.

### Seguridad de Teams

- Solo procesa los chats definidos en `config/settings.yaml`.
- No solicita ni almacena credenciales.
- No accede al terminal, disco o red desde Ollama.
- Se detiene ante un selector ambiguo, pantalla no reconocida o chat distinto.
- Siempre requiere aprobacion humana antes de insertar.
- Insertar no implica enviar: el envio exige una segunda confirmacion.

El perfil persistente de Edge y la base de datos se guardan bajo
`%LOCALAPPDATA%\TeamsLocalAgent`, fuera de la carpeta sincronizada del proyecto.
No copies ni sincronices esa carpeta. Teams puede cambiar su DOM sin aviso; sus
selectores deben validarse en un chat de prueba antes de reanudar esta función.

## Licencia

MIT. Ver [LICENSE](LICENSE).
