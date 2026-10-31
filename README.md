# Agente local

Agente local con aprobación humana obligatoria. Todos sus features deben estar
declarados en esta tabla; lo implementado debe ser consistente con este README.

| Feature | Estado | Módulo | Comando |
|---|---|---|---|
| Facturas de Hotmail | En implementación | `src/mail_invoices/` | `python -m mail_invoices.main --login` |
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

Se preparó un módulo independiente de Teams con extracción local de XML UBL,
ZIP y PDF con texto. Los PDF escaneados, enlaces externos y datos incompletos
requieren revisión manual; no se inventan valores ni se descargan enlaces.

El registro preparado utiliza `Hoja1`, encabezados en B2:E2 y añade
`Número de factura` en F2 solo al guardar un lote aprobado. Conserva las filas
existentes, comprueba números por proveedor y crea una copia de seguridad local
antes de reemplazar el Excel. Una coincidencia histórica sin número requiere
confirmación; no se completa por inferencia. No se mezclan monedas: el esquema
actual admite solo valores confirmados en COP.

El Excel real no se ha modificado. El escaneo de Outlook, las sugerencias de
carpetas y la interfaz de aprobación siguen pendientes de validar la sesión y
el DOM real de Outlook en Opera.
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

