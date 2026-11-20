# Desarrollo

Referencia para modificar el proyecto: tecnologías, organización del código,
pruebas y asistentes de código. Las reglas obligatorias (seguridad, aprobación
humana, privacidad, ramas y commits) están en [AGENTS.md](../AGENTS.md).

## Tecnologías

| Parte | Tecnología |
|---|---|
| Lenguaje | Python 3.12 o posterior; dependencias en `pyproject.toml` |
| Modelo local | Ollama (`qwen2.5:7b`), con un cliente propio sin herramientas |
| Outlook | Microsoft Graph con `msal`, una app compartida: `Mail.Read` (cierre, solo `GET`) y `Mail.ReadWrite` (facturas, solo leer y mover) |
| Gmail | API de Gmail con `google-auth-oauthlib` (solo inicio de sesión y renovación) y `httpx`, permiso `gmail.readonly` |
| Facturas | `openpyxl` (Excel), `defusedxml` (XML), `pypdf` (PDF con texto), `pypdfium2` y `Pillow` (renderizar PDF escaneados), Tesseract (OCR, instalación aparte) |
| Persistencia | SQLite de la biblioteca estándar |
| Zonas horarias | `zoneinfo` con `tzdata`, necesario en Windows para las zonas IANA y el horario de verano |
| Paneles | Servidores web locales en `127.0.0.1`, HTML y JS en línea sin recursos externos |
| Pruebas | pytest, `pytest-cov` y Playwright para Python (solo pruebas del panel; ninguna funcionalidad controla un navegador) |
| Lanzador | Windows PowerShell 5.1 (`scripts\agente-local.ps1`) |

## Organización del código

```text
src/
  shared/          lo que usan ambas funcionalidades; no depende de ninguna
    storage.py       carpeta de datos (.local o AGENTE_LOCAL_DATA)
    local_model.py   cliente de Ollama: solo direcciones locales, sin proxy ni herramientas
    run_log.py       registro por ejecución, conserva los 10 más recientes
  monthly_close/   cierre mensual (Graph, Gmail, extractos, panel)
  mail_invoices/   facturas (Graph, extracción, panel de revisión, Excel)
tests/
  conftest.py      dirige cada prueba a una carpeta de datos temporal
  shared/  monthly_close/  mail_invoices/
scripts/
  agente-local.ps1          lanzador único
  bootstrap.ps1             prepara .venv
  register-monthly-close.ps1  instala o quita la tarea programada
config/            solo plantillas; lo personal lo ignora Git
.local/            datos y configuración locales, ignorados por Git
```

Módulos de facturas (`src/mail_invoices/`):

| Módulo | Responsabilidad |
|---|---|
| `main.py` | Argumentos, registro de la ejecución y reporte de errores |
| `batch_workflow.py`, `batch_state.py` | Ejecución recuperable de lotes y sus estados |
| `batch_review.py` | Qué muestra el panel y las reglas de aprobación, sin interfaz |
| `review_server.py`, `review.html` | Panel web local de revisión |
| `batch_cleanup.py` | Limpieza de estados de lotes |
| `graph_mailbox.py` | Buzón por Graph: carpetas, candidatos, adjuntos, mover y verificar, sesión |
| `documents.py`, `ocr.py`, `assist.py` | Extracción de XML, PDF y OCR, y completado con Ollama |
| `folders.py`, `classification.py`, `classification_decisions.py` | Sugerencia de carpetas y ejemplos confirmados |
| `workbook.py` | Validación, duplicados, copia y escritura verificada del Excel |
| `review.py`, `settings.py`, `workflow.py` | Campos revisados, configuración y modos de solo lectura |

`InvoiceWorkbook.record(..., restore_amount_format=True)` restaura el formato de la
columna de valor en facturas ya registradas, sin agregar filas: solo admite
facturas cuyo número, proveedor, fecha e importe coinciden, y crea una copia y
verifica el libro antes de reemplazarlo.

Cada funcionalidad mantiene separados su acceso al buzón, su uso de Ollama, su
interfaz de aprobación, su persistencia y su configuración. Lo que use más de una
va en `src/shared/`.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pytest                       # todas
.\.venv\Scripts\python.exe -m pytest tests\monthly_close   # una funcionalidad
.\.venv\Scripts\python.exe -m pytest tests\mail_invoices --cov=mail_invoices --cov-report=term-missing
```

- No necesitan credenciales, correos ni libros reales: usan datos sintéticos,
  buzones de Graph y Gmail simulados y libros de Excel temporales.
- `tests/conftest.py` cambia `AGENTE_LOCAL_DATA` a una carpeta temporal en cada
  prueba: ninguna lee ni escribe `.local`.
- Las pruebas del panel usan Chromium con Playwright (lo instala
  `bootstrap.ps1`). Las del lanzador ejecutan PowerShell con un Python y un Ollama
  simulados.
- Para escribir una prueba del panel, usa las fixtures, no arranques nada a mano:
  - `new_page(**opciones)` (`tests/conftest.py`) abre una página en un contexto
    nuevo de un único Chromium por ejecución; lanzar uno por prueba costaba cerca
    de un segundo cada vez.
  - `start_panel(store, model=None)` (`tests/monthly_close/conftest.py`) arranca el
    panel del cierre y devuelve su URL; se detiene solo al terminar la prueba y su
    cierre no espera medio segundo.
  - Tras abrir o recargar el panel del cierre, espera
    `expect(page.locator("main")).to_have_attribute("aria-busy", "false")`, que
    marca el fin de la primera carga, en vez de `networkidle` (que espera medio
    segundo sin tráfico).
- Deben demostrar que nada se escribe ni se mueve sin aprobación, que lo aprobado
  coincide con lo escrito, que el agente se detiene ante errores de sesión o de
  buzón y que nunca ejecuta código del modelo.
- La configuración de pytest usa captura `sys`; no la cambies a `fd`.

## Registro de ejecuciones

`shared/run_log.py` copia a `logs\AAAAMMDD-HHMMSS-<comando>.log` lo que imprime
cada ejecución y, al terminar, deja los 10 registros más recientes (un registro
aún abierto se borra en una ejecución posterior). El lanzador crea el archivo y
añade sus líneas `[lanzador]`. Las barras de progreso no se guardan. Con
`capture_output=False` solo se guarda la salida de error, para los modos que
imprimen datos personales. Una parada deja su motivo y el archivo y la línea
donde ocurrió, sin traza completa.

## Asistentes de código

| Archivo | Quién lo lee | Contenido |
|---|---|---|
| [`AGENTS.md`](../AGENTS.md) | GitHub Copilot directamente; Claude Code a través de `CLAUDE.md` | Reglas del proyecto, seguridad y catálogo de skills |
| [`CLAUDE.md`](../CLAUDE.md) | Claude Code | Una línea, `@AGENTS.md`, porque Claude Code no lee `AGENTS.md` por sí solo |
| [`.claude/skills/`](../.claude/skills/) | Ambos | Única ubicación de las skills del proyecto |
| [`.claude/settings.json`](../.claude/settings.json) | Claude Code | Permisos compartidos: impide leer las sesiones de Microsoft y Google y el cliente OAuth de Gmail |

Para cambiar una regla, edita solo `AGENTS.md`. Los ajustes personales de Claude
Code van en `.claude/settings.local.json`, que Git ignora.

## Ramas y cambios

El repositorio sigue GitFlow (`main`, `develop`, `feature/*`, `release/*`,
`hotfix/*`) con mensajes Conventional Commits en español. Todo cambio a `develop`
o `main` entra por pull request, después de pasar `pytest`. Las reglas completas
están en [AGENTS.md](../AGENTS.md#version-control-gitflow).

Cada cambio de una funcionalidad actualiza en el mismo commit su sección del
[README](../README.md) y su guía en `docs/`.
