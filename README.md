# Agente local

Agente local con aprobación humana obligatoria. Toda funcionalidad nueva o
modificación solicitada se declara en este README en el mismo cambio en que se
implementa.

| Feature | Estado | Módulo | Comando |
|---|---|---|---|
| Facturas de Hotmail | En implementación | `src/mail_invoices/` | `python -m mail_invoices.main --login` / `--analyze-file` |
| Respuestas en Teams | WIP pausado | `src/app/`, `src/automation/`, `src/ai/`, `src/storage/` | `python -m app.main` |

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
  correo). La sugerencia debe ser una carpeta existente; nunca se crean carpetas.

Reglas de seguridad de esta ampliación:

- Ollama solo se acepta en `127.0.0.1`, `localhost` o `::1`, sin proxy ni
  herramientas; el texto del documento se trata como dato no confiable.
- El XML y las reglas deterministas prevalecen; el modelo nunca reemplaza un
  valor existente. Toda factura que no venga completa de XML, o con campos
  propuestos por el modelo, queda marcada `requires_review`.
- Ollama no registra datos, no mueve correos ni cambia carpetas. Se requerirá
  aprobación explícita antes de registrar un lote en Excel, y un correo solo se
  moverá después de verificar el registro o confirmar que es duplicado.
- No se inventan valores ni se descargan enlaces externos.

Pendiente: obtener del Outlook real las carpetas y el historial que alimentan
la sugerencia de carpetas, y conectar estas sugerencias con la interfaz de
aprobación y el movimiento de correos.

### Analizar un adjunto local (solo lectura)

Extrae los datos de un XML, PDF o ZIP guardado en el equipo y los muestra en
JSON, con el método usado (`xml`, `pdf_text`, `ocr`), los campos completados
por el modelo y los rechazados. No escribe en el Excel ni toca el correo.

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --config config\mail-invoices.yaml --analyze-file C:\ruta\factura.pdf
```

Usa `--no-model` para ver solo el resultado de reglas y OCR, sin Ollama.

### Requisitos de OCR

1. `winget install --id UB-Mannheim.TesseractOCR -e`
2. Descarga `spa.traineddata` de
   [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) a
   `%LOCALAPPDATA%\MailInvoiceAgent\tessdata` y copia ahí `eng.traineddata`
   desde `C:\Program Files\Tesseract-OCR\tessdata`.
3. Ajusta la sección `ocr` de `config/mail-invoices.yaml` si usas otras rutas.

Ollama debe estar en ejecución con el modelo configurado (`qwen2.5:7b` por
defecto) en la sección `ollama`.

El registro preparado utiliza `Hoja1`, encabezados en B2:E2 y añade
`Número de factura` en F2 solo al guardar un lote aprobado. Conserva las filas
existentes, comprueba números por proveedor y crea una copia de seguridad local
antes de reemplazar el Excel. Una coincidencia histórica sin número requiere
confirmación; no se completa por inferencia. No se mezclan monedas: el esquema
actual admite solo valores confirmados en COP.

El Excel real no se ha modificado. El escaneo de Outlook, la lectura de sus
carpetas e historial para las sugerencias, la interfaz de aprobación y el
movimiento de correos siguen pendientes de validar la sesión y el DOM real de
Outlook en Opera.
No ejecutar escrituras sobre el Excel real desde código de prueba: la futura
interfaz debe solicitar aprobación explícita del lote antes de llamar al registro.

La configuración personal está en `config/mail-invoices.yaml`, excluida de Git.
La plantilla es `config/mail-invoices.example.yaml`. El perfil de Opera y las
copias locales están bajo `%LOCALAPPDATA%\MailInvoiceAgent`, fuera de los
perfiles habituales de Opera y Teams.

Inicio de sesión manual (sin escanear ni mover mensajes):

```powershell
.\.venv\Scripts\python.exe -m mail_invoices.main --login --config config\mail-invoices.yaml
```

Opera se comprobó con Playwright en una página local. Esto no valida todavía
su sesión ni sus selectores en Outlook. Completa el acceso manual y cierra la
ventana; no compartas credenciales ni copies cookies.

## Teams: alcance pausado

Aplicacion local que prepara borradores de Teams Web y exige aprobacion humana antes de insertar o enviar texto.

## Seguridad

- Solo procesa los chats definidos en `config/settings.yaml`.
- No solicita ni almacena credenciales.
- No accede al terminal, disco o red desde Ollama.
- Se detiene ante un selector ambiguo, pantalla no reconocida o chat distinto.
- Siempre requiere aprobacion humana antes de insertar.
- Insertar no implica enviar: el envio exige una segunda confirmacion.

## Instalacion en Windows

1. Instala Python 3.12, Microsoft Edge y Ollama.
2. Ejecuta `.\scripts\bootstrap.ps1`.
3. Copia y ajusta la configuracion:
   `Copy-Item config/settings.example.yaml config/settings.yaml`
   Agrega solo el identificador y nombre exacto de un chat interno autorizado.
   La configuración de ejemplo no autoriza chats.
4. Inicia con `.\scripts\run.ps1`.

El perfil persistente de Edge y la base de datos se guardan bajo
`%LOCALAPPDATA%\TeamsLocalAgent`, fuera de la carpeta sincronizada del proyecto.
No copies ni sincronices esa carpeta.

Para iniciar sesion dentro del mismo contexto persistente de Playwright, ejecuta
`.\.venv\Scripts\python.exe -m app.main --process login --config config/settings.yaml`.
Completa el inicio de sesion/MFA manualmente en Edge y cierra la ventana. Este modo
no abre chats ni lee mensajes. El agente no pide ni maneja credenciales.
Si el siguiente inicio vuelve a pedir autenticacion, detente y consulta a TI: no uses
el perfil habitual ni intentes eludir politicas de acceso.

Teams puede cambiar su DOM sin aviso. Valida los selectores en `src/automation/selectors.py` en un chat de prueba antes de usar la aplicacion.

## Pruebas

`.venv\Scripts\python.exe -m pytest`

## Licencia

MIT. Ver [LICENSE](LICENSE).
