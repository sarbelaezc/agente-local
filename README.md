# Agente local

Agente local con aprobación humana obligatoria. Toda funcionalidad nueva o
modificación solicitada se declara en este README en el mismo cambio en que se
implementa.

| Feature | Estado | Módulo | Comando |
|---|---|---|---|
| Organización y clasificación de facturas electrónicas en Hotmail | En implementación; piloto supervisado con resultado mixto | `src/mail_invoices/` | `scripts/run-invoice-batch.ps1` / `-Resume` |
| Respuestas en Teams | WIP pausado | `src/app/`, `src/automation/`, `src/ai/`, `src/storage/` | `python -m app.main` |
| Cierre mensual de gastos de Outlook con Ollama local | Importación de octubre verificada con corte por fechas; panel e inversiones disponibles; conciliación y tarea pendientes | `src/monthly_close/` | `scripts/run-monthly-close.ps1` / `scripts/import-monthly-close.ps1` |

## Stack tecnológico

- **Python 3.12 o posterior**; empaquetado con `setuptools` y dependencias declaradas en `pyproject.toml`.
- **Playwright para Python** automatiza los navegadores. Facturas usa Opera; Teams usa un perfil separado de Microsoft Edge y permanece pausado.
- **Una instancia local de Ollama por feature**; `httpx` hace las llamadas y `PyYAML` gestiona la configuración existente. Teams usa `127.0.0.1:11435`; facturas usa `127.0.0.1:11434`; cierre mensual usa `127.0.0.1:11436` y configuración JSON independiente.
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

## Ejecución bajo demanda

La función de facturas no tiene servicio residente ni tarea programada. Para iniciar manualmente una
revisión de hasta 3 correos de los últimos 30 días:

```powershell
.\scripts\run-invoice-batch.ps1
```

Ajusta el tamaño (1 a 5) y el rango (1 a 365 días) cuando lo necesites; usa
`-Resume` para retomar aprobaciones persistidas sin volver a escanear:

```powershell
.\scripts\run-invoice-batch.ps1 -BatchSize 5 -Days 30
.\scripts\run-invoice-batch.ps1 -Resume -BatchSize 5
```

El script requiere `.venv`, `config/settings.yaml`, Opera y la instancia local
de Ollama de facturas en el endpoint configurado. Si falta la configuración,
crea una copia de la plantilla y se detiene para que completes los valores.
Cada ejecución abre la revisión supervisada; nada se escribe ni se mueve hasta
que selecciones y apruebes los correos en la UI. `scripts/run.ps1` sigue siendo
exclusivo de Teams y no debe usarse para facturas.
Si la política de PowerShell bloquea los scripts `.ps1`, ejecuta el mismo modo
directamente desde la raíz del repositorio:

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config/settings.yaml --process-invoice-batch --batch-size 3 --days 30
```

## Cierre mensual de gastos: Outlook Web y Ollama local

### Alcance y reglas

Nueva función separada de facturas y Teams. No requiere Microsoft Graph,
credenciales de API ni conector de Gmail. Playwright lee Outlook Web en un perfil
exclusivo. Usa un cliente local de Ollama con JSON Schema propio de este feature,
sin modificar las integraciones de facturas ni Teams.
No descarga adjuntos, envía correos, mueve mensajes ni modifica libros de facturas.
Abrir un mensaje puede marcarlo como leído en Outlook.

El panel local ofrece:

- **Paso 0:** las seis preguntas juntas: ubicación/zona horaria, cuentas, cuentas
  excluidas, etiqueta para transferencias, tratamiento de ahorros y transferencias
  a personas. Añade moneda, categorías, remitentes exactos y autorización para
  contenido confidencial. La lectura queda bloqueada sin completar este paso.
- **Mes:** meses seleccionables, total personal y familiar separados, calendario,
  dona por categoría, acumulado, días de la semana, medios de pago, comercios y detalle.
- **Año:** comparativa mensual, promedios, mes de mayor gasto, categoría líder y
  tabla personal/familiar/pendiente. Los meses no conciliados llevan advertencia.
- **Decisión:** confirmar gasto personal, familiar o no contable y corregir categoría.
  Las decisiones se guardan en SQLite y pueden revisarse después. Reimportar no
  las reemplaza.

Los importes se calculan con enteros en centésimas, no con aritmética del modelo.
No se convierten ni mezclan monedas; cada configuración importa una sola moneda,
con hasta dos decimales. Fechas ISO con zona explícita se convierten a la zona
IANA elegida. No se supone que una fecha de la interfaz de Outlook esté en UTC.
Si el aviso no contiene fecha de compra reconocible, se usa la fecha local del
aviso y se indica en la propuesta.

Ingresos, préstamos, compras rechazadas, promociones, códigos y pagos de tarjeta
no cuentan. Las compras de tarjeta se revisan individualmente. Transferencias
propias o cuentas excluidas solo permiten Familiar o No cuenta.
**Desviación deliberada del video:** toda propuesta de gasto requiere confirmación
humana, incluso si se eligió considerar transferencias personales por defecto.
Una clasificación de Ollama no equivale a aprobación. Revisa posibles avisos
duplicados de la misma compra antes de aprobarlos.

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

### Configuración y primer uso en Windows

1. Actualiza el entorno con `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`.
   La nueva dependencia `tzdata` permite zonas IANA y horario de verano en Windows.
2. Prepara la configuración técnica fuera de Git:

   ```powershell
   $data = Join-Path $env:LOCALAPPDATA "MonthlyCloseAgent"
   New-Item -ItemType Directory -Force $data | Out-Null
   if (-not (Test-Path (Join-Path $data "runtime.json"))) {
     Copy-Item config\monthly-close.example.json (Join-Path $data "runtime.json")
   }
   .\scripts\run-monthly-close.ps1
   ```

   Abre en el navegador la dirección local indicada en consola y completa el
   paso 0. No introduzcas números de cuenta, contraseñas ni tokens. Usa alias.
   Puedes dejar remitentes vacíos mientras los identificas; eso bloquea `scan`.
   El panel escucha exclusivamente en `127.0.0.1`, no se publica en Internet.
3. Configura `browser_executable` en `runtime.json` e inicia sesión manualmente:

   ```powershell
   .\scripts\run-monthly-close.ps1 -Command login
   ```

   Solo este comando permite la preparación manual del perfil. El agente no
   resuelve MFA, captcha ni autenticación. En lecturas posteriores se detiene si
   encuentra esos estados. No reutilices el perfil de facturas o el perfil habitual.
4. **Valida el recorrido antes de importar.** Desactiva la agrupación por
   conversaciones y la vista Prioritarios/Otros. El lector de origen MIME usa
   selectores centralizados en `source_reader.py`, observados en Outlook español;
   no depende de la búsqueda global ni del panel de lectura antiguo.

   ```powershell
   .\scripts\run-monthly-close.ps1 -Command scan -ReadOnly -Start 2026-10-01 -End 2026-11-01
   ```

   Comprueba explícitamente Bandeja de entrada, Correo no deseado, Elementos
   eliminados y **Dinero con todas sus subcarpetas**, incluidas las anidadas y
   todas sus páginas virtuales. Expande únicamente el árbol Dinero; identifica
   cada subcarpeta por su ruta completa, no solo por su nombre. Esta variante solo examina
   metadatos: no abre mensajes, no usa Ollama y no escribe cobertura ni decisiones.
   Las fechas no reducen este recorrido de metadatos. Tras verificarlo y validar
   el piloto MIME de abajo, establece `folder_scan_verified: true` en el archivo
   local. La plantilla lo deja en `false`; un cambio de DOM exige recalibración,
   nunca relajar controles ni omitir una carpeta que falle.
5. Aprueba en el panel las direcciones exactas de tus avisos bancarios. El comando
   `discover` (búsqueda global con selectores propios) se retiró: nunca se calibró
   para este buzón y el panel no mostraba sus hallazgos. Las claves `selectors` y
   `selectors_verified` que queden en un `runtime.json` existente se ignoran.
6. Inicia una tercera instancia de Ollama, separada de facturas (`11434`) y
   Teams (`11435`). En otra terminal:

   ```powershell
   $env:OLLAMA_HOST = "127.0.0.1:11436"
   $env:OLLAMA_MODELS = "$env:LOCALAPPDATA\MonthlyCloseAgent\OllamaModels"
   ollama serve
   ```

   En otra consola con las mismas variables, descarga el modelo configurado:
   `ollama pull qwen2.5:7b`. La descarga inicial necesita Internet; la inferencia
   usa únicamente loopback, sin proxies ni herramientas para el modelo.
   `scan` (sin `-ReadOnly`), la tarea programada y `pilot` comprueban que esta
   instancia tiene el modelo configurado **antes** de abrir Outlook; si no, se
   detienen sin recorrer carpetas ni marcar cobertura.
7. Importa de enero al mes anterior con `-Command scan` sin fechas, o indica
   `-Start` y `-End` (fin exclusivo). Ejemplo:

   ```powershell
   .\scripts\run-monthly-close.ps1 -Command scan -Start 2026-01-01 -End 2026-11-01
   ```

   El límite predeterminado es **200 filas en total entre todas las carpetas del alcance**,
   incluidas las de remitentes no autorizados. Se permite completar exactamente
   ese total; si falta otra fila, se detiene con error, sin saltarse carpetas.
   Solo se abre el origen de remitentes aprobados. Su fecha RFC de recepción se
   convierte a la zona local: los avisos desde `End` en adelante se omiten, los
   de `Start` hasta antes de `End` se procesan y el primer aviso anterior a
   `Start` termina **esa carpeta**, no todo el recorrido. Se continúa con la
   siguiente carpeta, incluidas las subcarpetas de Dinero. El corte exige confirmar
   en Outlook el orden «Más recientes en la parte superior» y que no haya huecos
   en las filas previas; si no se confirma, se detiene con error. No se envían a
   Ollama avisos fuera del rango. Por tanto, las fechas sí acotan el recorrido
   histórico; los avisos más recientes y los remitentes no autorizados previos
   siguen consumiendo el límite. `--read-only` mantiene el recorrido completo de
   metadatos, porque no abre orígenes para conocer sus fechas.
   Las carpetas personalizadas fuera del árbol Dinero no se recorren.
   Si falta Dinero, falla una expansión o cambia su árbol durante el recorrido,
   se detiene sin declarar cobertura completa ni omitir subcarpetas.
   Los mensajes ya importados no se duplican por `Message-ID`, incluso si aparecen
   en otra carpeta. Si una ejecución falla, conserva lo ya importado como parcial;
   puedes repetirla sin sobrescribir decisiones. Pulsa Actualizar en el panel.
   Para el mes vivo, incluye sus fechas explícitamente.

### Importación masiva desde una terminal de Windows

**Última validación:** el recorrido de octubre (`2026-10-01` a `2026-11-01`,
fin exclusivo) terminó con código 0 tras incorporar el corte por fecha.
Se verificaron 15 carpetas y 1517 filas de metadatos; Bancolombia, Dian e
Inversiones terminaron al encontrar un aviso anterior al inicio. No se recorrió
innecesariamente el resto de su histórico. Las carpetas sin remitentes aprobados
solo se revisaron por metadatos, sin abrir sus correos.
Octubre quedó con 41 movimientos y 2 decisiones pendientes. Las 39 decisiones
confirmadas conservaron exactamente sus identificadores, categorías y valores;
SQLite pasó su comprobación de integridad y se limpiaron los puntos de reanudación.
El recorrido está validado, pero octubre sigue marcado parcial hasta conciliar
contra extractos: no se afirma cobertura de otras carpetas ni de avisos inexistentes.
Pasaron las 423 pruebas del repositorio, incluidas fechas locales, fin exclusivo,
corte por carpeta, rechazo de orden no verificado y preservación de decisiones.
Los resultados fallidos descritos más abajo son intentos anteriores.

Las ejecuciones interrumpidas guardan un punto de reanudación en la tabla local
`scan_progress`: huellas SHA-256 de carpeta, fila y metadatos exactos, con estado
pendiente o completado, sin cuerpos ni asuntos en texto claro. Al repetir el
mismo rango y configuración, se verifica nuevamente todo el árbol y las filas
hasta el corte por fecha de cada carpeta,
pero no se vuelve a abrir el origen de avisos completados cuyos metadatos no
cambiaron. El aviso que falló queda pendiente antes de abrirlo y se vuelve a
intentar al encontrarlo. Si cambian sus metadatos o identificador, se lee de nuevo,
nunca se salta por posición o por la fecha de una decisión. Cambiar configuración
o rango inicia otro ámbito. Al terminar el recorrido se borra su progreso.
No se garantiza reanudar exactamente en la misma fila si Outlook cambia sus
identificadores. No se deduce una fecha del aviso cuando no se pudo abrir su
origen. Las decisiones existentes siguen protegidas por `Message-ID`.
Los fallos anteriores a esta implementación no tienen punto retroactivo.
Validación real: el primer intento guardó progreso y la segunda ejecución
identificó y reintentó el aviso pendiente. Quedaron 71 avisos completados en el
punto de reanudación y uno pendiente; octubre llegó a 39 movimientos, con dos
decisiones pendientes. El reintento volvió a fallar en la interfaz de Outlook:
el mecanismo de reanudación funciona, pero el recorrido continúa parcial.
Pasaron las 412 pruebas del repositorio. La reanudación no corrige por sí sola
un fallo persistente de apertura del menú.

Desde la raíz del proyecto, en **PowerShell**:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\scripts\import-monthly-close.ps1 -Start 2026-01-01 -End 2026-11-01
```

El cambio de política solo afecta a esa terminal y desaparece al cerrarla.
No requiere permisos de administrador ni modifica la política del equipo;
una política de organización puede impedirlo y debe respetarse.
No hay un lanzador Python adicional ni dependencia de Bash.

Desde **CMD**, puedes ejecutar el mismo script:

```cmd
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File scripts\import-monthly-close.ps1 -Start 2026-01-01 -End 2026-11-01
```

Desde **Git Bash**:

```bash
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File scripts/import-monthly-close.ps1 -Start 2026-01-01 -End 2026-11-01
```

El fin es exclusivo. Sin fechas importa desde enero del año local hasta el mes
anterior. Usa `-End 2026-11-03` para incluir el 2 de noviembre.
Para importar solo desde octubre hasta esa fecha:

```powershell
.\scripts\import-monthly-close.ps1 -Start 2026-10-01 -End 2026-11-03
```

Cuando el correo contiene montos con símbolo monetario o el código configurado,
el esquema de Ollama limita `amount_text` a esos textos literales. Esto no aprueba
la interpretación: se mantiene la verificación posterior de evidencia y la
decisión humana. Si falla, el diagnóstico indica el campo, nunca su contenido.
Para el comercio se admite recuperar un único fragmento original que difiera
solo en espacios, tabulaciones o saltos de línea: se guarda el texto literal del
correo, no la versión reformateada por Ollama. No se corrigen letras, mayúsculas,
acentos, montos ni coincidencias ambiguas; esos casos siguen deteniendo la
importación. Esta recuperación evita rechazar nombres partidos por el formato
MIME, pero no garantiza que cualquier correo pueda extraerse correctamente.
Si aún falla la evidencia literal de comercio o monto, se registra una advertencia
sin contenido financiero y se solicita **una sola corrección local** a Ollama con
el campo inválido y la propuesta previa. Se vuelve a validar toda la respuesta;
la corrección no puede eliminar movimientos ni cambiar otros campos. Nada se
guarda hasta superar la validación. Si vuelve a fallar, se detiene y el correo
queda sin marcar como procesado. No hay reintentos ilimitados ni omisión automática.
El contexto de Ollama distingue los avisos «Pagaste [monto] en la tarjeta de
crédito [tarjeta] desde la cuenta [cuenta]» como abonos a la deuda, no compras:
deben producir `events: []` para evitar contar dos veces el gasto. La regla no
depende del monto, marca ni terminación de tarjeta. Un aviso «Compraste … con tu
tarjeta» sí se extrae; mencionar una tarjeta no basta para excluirlo. Los casos
ambiguos siguen como `unknown` para revisión. Los ejemplos del código son
ficticios, sin datos bancarios del usuario. Esto se aplica a nuevas extracciones:
no cambia movimientos ya importados ni decisiones previas; en esos casos revisa
el historial y elige **No cuenta** para los abonos ya identificados.
Los ejemplos se envían como `approved_notice_examples`, separados de
`untrusted_email`: son contexto aprobado, no evidencia ni movimientos a importar.
Con Ollama local se verificaron dos abonos ficticios (con y sin tilde en
«crédito») y una compra de control: los abonos devolvieron cero eventos y la
compra produjo una propuesta pendiente. No se reprodujeron ni modificaron
automáticamente los movimientos bancarios ya importados del usuario.
La recuperación de espacios y la corrección dirigida se verificaron con correos
reales que bloqueaban octubre. En la última validación se solicitó una corrección,
se superó la validación y el recorrido continuó: se procesaron 14 correos adicionales
y octubre pasó de 15 a 24 movimientos, con 2 decisiones pendientes al finalizar.
Las 379 pruebas del repositorio pasaron. El recorrido completo no terminó:
Outlook se detuvo al abrir el menú Ver (`source_reader.py:453`).
Octubre y noviembre continúan parciales; no se declara terminada la importación.
El diagnóstico posterior comprobó que Outlook puede mantener el origen visible
con `aria-hidden="true"`. La lectura y la espera de cierre ahora usan el diálogo
DOM único y su visibilidad física, sin perder las validaciones de selección,
remitente, asunto y encabezados RFC. Un diálogo previo, incluso oculto a
accesibilidad, bloquea una nueva apertura. El submenú de origen se activa con
Enter tras abrirlo con ArrowRight, evitando mover el puntero para activarlo.
Se añadieron regresiones del diálogo oculto a accesibilidad, cierre asíncrono
y pérdida del submenú por movimiento del puntero. Una prueba real sin Ollama
leyó 50 avisos consecutivos, pero las importaciones completas siguen deteniéndose
por desaparición del menú y retorno del foco a la fila. Con autorización del
usuario se habilitó seleccionar primero el asunto mediante clic normal, esperar
la selección y revalidar la identidad antes de abrir el menú contextual.
Esto puede marcar como leídos los avisos autorizados que se abran; el filtro de
fechas ocurre después de leer el origen, por lo que también puede afectar avisos
autorizados fuera del rango. No envía, mueve ni borra correos. Se mantienen los
controles de remitente, selección y carpeta; no hay reintentos que oculten fallos.
Con la vista de lectura completa, Outlook conserva el asunto en `textContent`
aunque `innerText` quede vacío. Se verifica el asunto desde el DOM y se cierra
esa vista solo si existe un único botón Cerrar y ningún diálogo; se espera la
reaparición del asunto en la lista y se revalida la selección. El menú de la fila
seleccionada se abre con Shift+F10 y el origen con ArrowRight y Enter.
Si el menú desaparece antes de activar el origen, se permite una sola reapertura
tras un timeout, con advertencia sin datos financieros. Solo se reintenta si no
queda ningún menú visible ni diálogo, y se revalidan buzón, carpeta, remitente,
asunto e identidad de la fila seleccionada. Se repite la selección del mismo aviso: si está abierta su vista completa,
se cierra con su único botón Cerrar, se verifica que reaparezca en la lista,
se vuelve a abrir y se restablece la lista antes de reabrir el menú.
Ese segundo intento usa clic derecho sobre la misma fila verificada, en lugar de
repetir Shift+F10.
No se repite la extracción; no se reintentan la activación del origen, su lectura ni errores de
autenticación o ambigüedad. Si el segundo intento falla, se detiene.
La comprobación de menú abierto usa contenedores `role="menu"`, no cualquier
`role="menuitem"` de la página: Outlook también usa este último para el calendario
y las barras de herramientas. Esos controles no deben bloquear la recuperación.
La vista completa se reconoce por su botón Cerrar, no solo por `innerText` del
asunto: la lista puede seguir renderizada detrás de ella. Se espera el cierre
físico y se restaura la lista también después de cerrar el origen, porque la
vista de lectura puede aparecer con retraso durante la apertura del menú.
Tras seleccionar el aviso se espera primero su botón Cerrar y después la barra
de acciones cargada (`More items` en la interfaz verificada). No se da por abierta
la vista solo porque la fila ya esté seleccionada. Un cambio de esa interfaz
detiene el recorrido en vez de continuar con el menú sobre contenido oculto.
La recuperación de cerrar y reabrir el aviso está cubierta con una prueba de
vista completa que aparece al cerrarse el menú. Pasaron 413 pruebas del
repositorio. El intento real posterior volvió a detenerse esperando Ver, sin
activar la recuperación porque no cumplió sus condiciones de seguridad:
no se considera solucionado el bloqueo de Outlook ni completado octubre.
La validación real de octubre exclusivamente (`2026-10-01` a `2026-11-01`,
fin exclusivo) activó esa única reapertura, pero el segundo intento volvió a
fallar esperando Ver. El proceso terminó con código 1; octubre permanece
parcial, con 37 movimientos y ninguna decisión pendiente al comprobarlo.
Pasaron las 410 pruebas del repositorio y la base de datos conservó su integridad.
No se creó commit ni se hizo push: la condición de completar el recorrido real
todavía no se cumple.
El intento real avanzó y alcanzó una nueva corrección de evidencia, pero volvió
a detenerse esperando el menú Ver. El fallo intermitente no está resuelto ni
la importación completa validada. Los errores de validación muestran solo
archivo y línea de los dos últimos puntos internos, sin contenido financiero.

Las instrucciones del proyecto incluyen ahora una regla explícita de no asumir
hechos ni requisitos, verificar con evidencia y preguntar al usuario antes de
actuar cuando quede una duda o se requiera una decisión.
El script usa la configuración privada predeterminada de MonthlyCloseAgent,
su entorno `.venv` de Windows y el perfil exclusivo de Edge. Comprueba la
configuración y el modelo instalado antes de importar. Si Ollama no responde,
inicia la instalación local del usuario con el puerto configurado y el directorio
de modelos exclusivo; no descarga modelos. Al terminar o fallar cierra únicamente
la instancia de Ollama que él inició. Una instancia preexistente queda abierta.
Al terminar el intento de importación, incluso si el escaneo devuelve un error,
inicia el panel en otra terminal y abre `http://127.0.0.1:8766` en el navegador
predeterminado. Si ya está activo, lo reutiliza. Espera su respuesta antes de
abrir la página y rechaza una página distinta en ese puerto. Mantén la terminal
del panel abierta mientras revisas. Conserva el código de error de la importación;
abrir el panel no significa que el recorrido haya terminado correctamente.
No instala tareas. No interactúes con el perfil de Edge durante la importación.
Los movimientos nuevos requieren revisión humana.

Para comprobar solo el recorrido, añade `-ReadOnly`; no abre el panel, no importa ni usa el
modelo para inferencia, aunque este lanzador también verifica su disponibilidad.
Si se interrumpe, puedes repetir el mismo comando sin sobrescribir decisiones.
Mantén la terminal abierta hasta que termine. Las primeras importaciones masivas
con Ollama siguen pendientes de validación real.

El script restaura las variables de entorno que modificó al terminar.
La comprobación del panel usa HTTP local sin proxy. El servidor atiende
conexiones concurrentes para que una conexión anticipada e inactiva del navegador
no bloquee la página ni sus comprobaciones de disponibilidad.
Se validó el arranque y cierre reales de Ollama con un rango deliberadamente
inválido, sin abrir correos. Las pruebas con un servicio simulado cubren la
reutilización de instancias, argumentos, código de salida y modelo ausente.
La importación masiva con correos reales no forma parte de estas pruebas.
La apertura automática del panel cuenta con siete casos verificados: importación
exitosa o fallida, panel existente o iniciado por el script, puerto ocupado por
otra página, modelo ausente y modo de solo lectura. Estas pruebas simulan la
apertura del navegador; no vuelven a ejecutar la importación real.

### Cobertura, cierre y tarea persistente

Cada mes importado se marca **parcial/sin conciliar**, aunque no haya avisos. Nunca
se certifica completitud contando correos. Se recorren Entrada, Spam, Eliminados y
Dinero con todos sus descendientes directamente; no se afirma cobertura de otras carpetas ni de mensajes ya borrados
por retención. Outlook puede cambiar sus controles. Compara los totales con
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
abren el navegador. Requiere Windows con sesión interactiva abierta, equipo
encendido y Ollama funcionando. Si falta autenticación o cambia Outlook, falla
sin continuar. No instala servicios ni tareas sin ejecutar este script.

Cada ejecución carga desde disco las respuestas, reglas, categorías y remitentes.
No depende de memoria de chat ni de enlaces publicados. SQLite conserva el
histórico y las decisiones; el panel refleja el mes nuevo sin republicación.
El resumen queda en `last-close.txt` dentro del directorio privado de datos.
También se consulta con:

```powershell
.\scripts\run-monthly-close.ps1 -Command summary -Month 2026-10
.\scripts\register-monthly-close.ps1 -Remove
```

El resumen indica gasto confirmado, familiar y dudas, no totales del modelo sin
aprobar. Un cierre a las 22:00 no incluye compras posteriores: vuelve a importar
ese mes al día siguiente antes de conciliarlo. No hay envío de notificaciones
por correo ni garantía de ejecución si el equipo está apagado.

### Privacidad, límites y validación

- `scan` inspecciona remitente, identificador y posición de todas las filas para
  verificar el recorrido. Solo lee asunto y origen MIME de remitentes aprobados;
  los demás no se abren ni se mandan a Ollama. No se transmiten
  mensajes a servicios de IA externos.
- El navegador sí se comunica con Microsoft. El modelo no dispone de terminal,
  disco, red ni herramientas; sus respuestas JSON se validan y jamás se ejecutan.
- No se guardan cuerpos en SQLite ni en logs. Se retienen los hechos financieros,
  decisiones y auditoría local hasta borrado
  manual. El perfil de navegador conserva sesión y caché administradas por el
  navegador; protégelo como información sensible.
- Para borrar todos los datos de esta función: detén panel, lecturas y tarea;
  elimina manualmente `close.sqlite3` del directorio de datos. Se pierden también
  decisiones e histórico. Borra `BrowserProfile` para retirar sesión y caché.
  No afecta a facturas ni Teams. No se realiza borrado automático del histórico.
- No hay cifrado propio ni autenticación multiusuario. Usa una cuenta Windows
  protegida y cifrado de disco si procede. Otros procesos del mismo equipo pueden
  acceder al panel local. No guardes el directorio de datos dentro del repositorio
  ni en una carpeta compartida o sincronizada.

Pruebas reproducibles sin credenciales ni correo personal:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\monthly_close\test_monthly_close.py tests\monthly_close\test_monthly_close_outlook.py tests\monthly_close\test_monthly_close_dashboard.py
```

Se validan reglas, importes, zona horaria, decisiones persistentes, exclusión de
remitentes no autorizados, protección del panel y DOM sintético con Playwright.
También se prueban el formulario inicial, los cambios de configuración, los
totales con centavos, las decisiones desde el navegador y las etiquetas de los
gráficos. Se revisaron capturas a 1440 y 390 píxeles, en modo claro y oscuro.
Las pruebas gráficas usan Chromium instalado por `bootstrap.ps1`.
**Pendiente de validar con el usuario:** selectores de su Outlook, cobertura real
de Spam/Eliminados, resultados del modelo local y ejecución de la tarea instalada.
Durante la implementación inicial no se accedió al buzón ni se configuró una
tarea real. Las validaciones posteriores con Outlook se detallan abajo.

### Piloto controlado con el Outlook real

El comando `pilot` permite validar **como máximo un aviso visible de un remitente
autorizado**, sin habilitar el lector general ni afirmar cobertura de carpetas.
Requiere el paso 0 completo, sesión manual y `runtime.json`. Prioriza el orden de
los remitentes configurados y elige su primera fila visible. No busca mensajes
adicionales ni lee cuerpos de remitentes fuera de esa lista.

```powershell
.\scripts\run-monthly-close.ps1 -Command pilot -ReadOnly
.\scripts\run-monthly-close.ps1 -Command pilot
```

La primera variante verifica lectura e identidad en memoria, sin Ollama ni
importación. La segunda comprueba primero que la instancia exclusiva tiene el
modelo configurado y luego propone movimientos para revisión humana. Repetirla
no duplica el mismo `Message-ID` ni reemplaza decisiones. Los avisos de dinero
recibido y otros conceptos no contables deben devolver cero propuestas. Una
propuesta sin evidencia literal o fuera de las categorías autorizadas se rechaza:
el piloto se detiene, no rellena ni acepta datos inventados.

En equipos que bloqueen scripts de PowerShell, puede usarse la entrada Python
equivalente, sin modificar la política de ejecución:

```powershell
.\.venv\Scripts\python.exe -m monthly_close.main serve
.\.venv\Scripts\python.exe -m monthly_close.main pilot --read-only
.\.venv\Scripts\python.exe -m monthly_close.main pilot
.\.venv\Scripts\python.exe -m monthly_close.main scan --read-only --start 2026-10-01 --end 2026-11-01
```

El adaptador del piloto, `source_reader.py`, usa selectores centrales observados
en la interfaz española: el `id` único del elemento **solo para identificar la fila**
y `span.TtcXM` para su asunto. `data-convid` identifica conversaciones y puede
repetirse entre mensajes individuales: no se usa como identidad única.
Valida nuevamente remitente y asunto antes de abrir
el menú contextual **Ver > Ver origen del mensaje**. Espera la selección y la
carga del origen por eventos del DOM, sin retardos fijos. No depende del panel
de lectura, que puede ocultar la lista o mantener contenido anterior.

El diálogo de origen debe ser único y nuevo. Se cotejan su remitente y asunto
RFC con la fila autorizada. El identificador persistido deriva del `Message-ID`
RFC, no de la conversación. La fecha de recepción se toma del primer encabezado
`Received`, con zona explícita; también se valida la zona de `Date`. No se
interpreta la fecha visual localizada ni se presupone UTC.

El origen MIME completo se carga temporalmente en memoria (máximo 2 MB). Se
decodifica solo su parte de texto, o HTML convertido a texto sin scripts, estilos
ni recursos remotos. No se decodifican ni guardan adjuntos. Se rechazan orígenes
defectuosos, encabezados críticos duplicados, identidades contradictorias y filas
con señales de conversación agrupada. Un fallo de Playwright no imprime su
registro de localizadores, porque puede incluir contenido financiero en
`aria-label`. El formato de estos controles depende de Outlook: un cambio detiene
el piloto, no activa una alternativa silenciosa.

Pruebas del nuevo adaptador y de la CLI controlada:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\monthly_close\test_monthly_close_folders.py tests\monthly_close\test_monthly_close_source.py tests\monthly_close\test_monthly_close_outlook.py tests\monthly_close\test_monthly_close.py tests\monthly_close\test_monthly_close_dashboard.py
```

**Prueba controlada posterior (2026-11-02): resultado parcial, escaneo bloqueado.**
Se confirmó la sesión manual en el perfil exclusivo. El buscador real usa
`#topSearchInput[role="combobox"]`; las filas usan `data-convid`, no `data-itemid`,
y exponen el remitente en un `span[title]`. Se abrió un solo aviso después de
verificar su remitente exacto contra la lista autorizada y la unicidad de su
identificador. No se extrajo el cuerpo, llamó a Ollama ni importaron movimientos.
Abrirlo puede haberlo marcado como leído.

El panel abierto mostró un único elemento `role="document"`, pero no se verificó
un identificador compartido con la fila, un remitente exacto dentro del panel ni
una fecha con zona explícita. Tampoco se verificó el alcance de Todas las carpetas.
Estos datos no permiten activar el lector actual: necesita adaptación y nuevas
pruebas, sin sustituir el identificador de mensaje por uno de conversación sin
validar los hilos. `selectors_verified` sigue en `false`; no se instaló una tarea.

**Corrección y repetición del piloto (2026-11-02):** se verificó un aviso real
mediante origen MIME, remitente/asunto concordantes, `Message-ID` y recepción con
zona horaria. La prueba de solo lectura terminó correctamente. Se levantó la
instancia exclusiva de Ollama en `11436` y se descargó el modelo configurado en
su directorio separado (aproximadamente 4,7 GB para `qwen2.5:7b`).
La primera salida del modelo se rechazó por falta de evidencia literal; no se
importó. Se aclaró el prompt para omitir ingresos y copiar los montos exactamente.
La repetición completa terminó correctamente con **cero propuestas**, porque
el aviso era de dinero recibido. No se contaron gastos ni se aprobaron decisiones.
Además, una compra sintética procesada por esa misma instancia real de Ollama
produjo una propuesta con importe exacto en centavos, sin aprobación ni escritura
en la base personal. Las 98 pruebas de cierre mensual pasaron tras estos cambios.
Este resultado no valida aún la extracción de una compra real, la cobertura de
Spam/Eliminados ni la tarea programada. El escaneo general permanece bloqueado.

**Validación posterior de una compra real (2026-11-02):** entre las filas visibles
se identificó un posible aviso de compra únicamente después de verificar su
remitente exacto contra la lista autorizada. Se procesó un solo aviso mediante
el adaptador de origen MIME y la instancia local de Ollama. Se obtuvo una
propuesta de compra con comercio y monto respaldados literalmente por el correo.
El importe se persistió en centavos y se comprobó su coincidencia entre SQLite
y la API del panel, junto con categoría y decisión.
La propuesta quedó **pendiente**, sin sumar a los totales confirmados ni registrar
aprobación humana. El mes se marcó parcial; esta prueba no representa el gasto
mensual completo. Se abrió el panel para que el usuario revise la propuesta.
Falta su comprobación manual de comercio, cuenta, categoría e importe; la
validación automática no sustituye esa decisión. No se verificaron cobertura de
Spam/Eliminados ni tareas programadas y el escaneo general continúa bloqueado.
No se incluyen datos financieros personales en este registro de validación.

**Persistencia y navegación de carpetas (2026-11-02):** después de que el usuario
eligió Familiar, se releyó el mismo aviso real y se reconoció su `Message-ID`.
No se llamó nuevamente al modelo ni se reimportó el mensaje. Se verificó que
movimiento, importe, categoría, decisión y cantidad de registros de aprobación
permanecieron intactos. La API del panel mantiene un movimiento y cero dudas.

Se verificó la navegación mediante controles únicos de Outlook a Correo no
deseado y Elementos eliminados, comprobando `aria-selected="true"` y el origen
esperado. Sus atributos observados son
`data-folder-name="correo no deseado"` y
`data-folder-name="elementos eliminados"` dentro de `NavigationPane`.
No se abrieron mensajes de esas carpetas ni se importaron movimientos nuevos.
Esto valida únicamente navegación: **no demuestra que la búsqueda global incluya
esas carpetas ni que se hayan leído todos sus avisos**. Tampoco se verificó la
cobertura histórica o una tarea programada. `selectors_verified` sigue en `false`.

**Recorrido explícito posterior de las tres carpetas:** `scan --read-only`
completó 48 filas de Entrada (14 de remitentes autorizados), 0 de Spam y 54 de
Eliminados (0 autorizadas). Se excluyó el acceso duplicado de Favoritos y se
verificó que los tres controles pertenecieran al mismo buzón. El lector compara
el total anunciado por cada carpeta con todos los índices virtuales recorridos:
los atributos ARIA de las filas no enfocadas permanecen en cero en esta versión
de Outlook y no sirven como posición. Se detiene ante huecos, cambios de orden,
cambios de total, límite, carpeta ausente, vista filtrada o controles ambiguos.

En este recorrido no se abrieron mensajes, no se llamó a Ollama ni se importaron
movimientos. Las 117 pruebas de cierre mensual pasaron. Las pruebas con interfaz
sintética cubren lectura MIME durante
paginación, avisos en las tres carpetas, rango por fecha local, duplicados y
conservación de la decisión Familiar. La lectura real de un aviso desde Spam
o Eliminados y la importación masiva con Ollama siguen pendientes; Spam estaba
vacío y Eliminados no contenía remitentes autorizados. No se ha validado cobertura
histórica ni instalado una tarea. La búsqueda global antigua permanece bloqueada
con `selectors_verified: false`. Se habilitó `folder_scan_verified` únicamente en
la configuración local tras validar el recorrido; la plantilla lo conserva
desactivado. La decisión Familiar y cero dudas quedaron intactas.

**Ampliación solicitada: Dinero y todas sus subcarpetas.** El recorrido incluye
ahora la carpeta Dinero y sus descendientes a cualquier profundidad. Despliega
los nodos contraídos mediante su control de expansión y verifica jerarquía,
identidad por ruta y pertenencia al mismo buzón. Permite nombres iguales en
ramas distintas, pero no hermanos ambiguos. Repite el inventario al terminar
para detectar cambios del árbol. No abre carpetas ajenas a este alcance.

La inspección real encontró 11 subcarpetas de Dinero y 8.194 filas anunciadas
entre las 15 carpetas del alcance (8.092 dentro de Dinero). Por ese volumen se
amplió el máximo configurable de `max_messages` a 10000 y se ajustó el límite
local a ese valor; la plantilla conserva 200. Las fechas siguen aplicándose
después de validar el origen de cada aviso autorizado, no reducen el recorrido
de metadatos. Un recorrido o una importación de este tamaño puede tardar varios
minutos. La verificación local se desactivó durante la recalibración y se
restableció tras completar el recorrido ampliado.

Se adaptaron los totales con separador de miles de Outlook español y el estado
vacío de carpetas personalizadas, siempre cotejado con total cero y selección
exacta. En listas grandes, la cantidad de filas virtuales puede cambiar durante
la iteración: se captura una instantánea de identidades y posiciones y se exige
verificar todos los índices, sin declarar éxito si desaparecen filas sin cubrir.
La identidad de fila usa el `id` del elemento; el `Message-ID` MIME sigue siendo
la única identidad persistida para deduplicar y conservar decisiones.

La validación real de solo metadatos completó las **15 carpetas y 8.194 filas**,
incluidas las 11 subcarpetas y 8.092 filas de Dinero. Encontró 1.683 filas de
remitentes autorizados, sin abrir sus mensajes ni llamar a Ollama. Pasaron
**137 pruebas** de cierre mensual, incluidas expansión recursiva, ramas con
nombres iguales, límites, cambios del árbol, instantáneas virtuales e identidades
de fila independientes de la conversación. No se importaron movimientos nuevos;
la decisión Familiar y cero dudas siguen intactas. La importación masiva real,
la conciliación histórica y la tarea programada permanecen pendientes.

**Primer intento de importación masiva real:** se detuvo de forma segura cuando
Outlook no abrió el menú contextual al pulsar una zona vacía de una fila.
Se corrigió el clic derecho para apuntar al asunto previamente validado.
La repetición acotada leyó 15 orígenes autorizados y superó la fila que fallaba,
sin llamar al modelo ni importar movimientos. No se relajan las comprobaciones
de remitente, selección ni identidad MIME.

**Resultado posterior de la validación masiva (enero a octubre de 2026):**
la ejecución con Ollama real se detuvo al rechazar una propuesta por
`Tipo de movimiento invalido` (el tipo o el indicador booleano de ahorros no
cumplía el contrato). No se normalizó ni aceptó ese resultado y el aviso fallido
no se marcó como procesado. La base conserva seis mensajes procesados en total,
un solo movimiento con decisión Familiar y cero dudas, sin nuevas propuestas
financieras importadas en estos intentos. Los meses del rango siguen parciales;
no se completó el recorrido con inferencia ni se certificaron sus totales.
Ollama y el navegador de la prueba quedaron cerrados. Es necesario validar la
salida del modelo para ese aviso antes de repetir una importación completa;
no se habilita saltar avisos inválidos para aparentar éxito.

**Preparación de uso y repetición desde el lanzador:** se ejecutó
`scripts\import-monthly-close.ps1` contra el buzón real. Se verificaron el
arranque y cierre de su instancia de Ollama, la apertura real del panel y la
reutilización de un panel existente. El panel queda disponible para revisión
manual aunque el escaneo termine con error; no equivale a importación completa.

La generación del cierre mensual usa ahora un cliente propio con JSON Schema:
exige campos tipados (incluido `savings` booleano), tipos de movimiento válidos,
categorías y cuentas configuradas, moneda configurada y fechas ISO o DD/MM/YYYY
que estén literalmente presentes en el texto (o fecha vacía para la regla de
fecha de recepción ya existente). No modifica el cliente de facturas ni el de
Teams. Esto restringe la forma de la propuesta; **no garantiza que sea cierta**.
Se siguen validando comercio, importe y fecha contra el aviso, y ninguna propuesta
de gasto se aprueba automáticamente.

La prueba real avanzó y guardó nuevas propuestas válidas, pero volvió a detenerse
ante un importe o comercio sin evidencia literal. También se observó un fallo
intermitente al recuperar el texto del diálogo de origen de Outlook.
Se usa navegación de teclado para desplegar el submenú y los errores de interfaz
indican solo archivo y línea del punto de parada, sin registros de localizadores
que puedan contener información financiera. No se omiten avisos fallidos ni se
marcan los meses como completos. **El lanzador y el panel son utilizables, pero
la importación histórica masiva todavía no está validada de principio a fin.**
Tras estas correcciones pasaron **152 pruebas** de cierre mensual. Se comprobó
el panel real con una conexión inactiva adicional, su API y la integridad SQLite.
La base contiene 13 avisos procesados y seis movimientos; las decisiones siguen
bajo control humano. Los diez meses de enero a octubre continúan parciales.
Ollama temporal quedó cerrado y el panel quedó abierto para revisión.

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
Las pruebas se agrupan por feature: `tests/mail_invoices/` para facturas de
Hotmail, `tests/monthly_close/` para cierre mensual y `tests/teams/` para Teams.
`tests/shared/` contiene las pruebas de configuración compartida entre features.
Esta organización no cambia el comportamiento de las funciones ni activa Teams.
Ejecuta todas con `.\.venv\Scripts\python.exe -m pytest`, o solo una feature con
`.\.venv\Scripts\python.exe -m pytest tests\monthly_close` (sustituye la carpeta
para probar otra feature).
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
