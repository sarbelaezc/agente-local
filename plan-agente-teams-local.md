# Plan de implementación: agente local para responder chats de Microsoft Teams

Fecha: 30 de octubre de 2026

## Estado actual: WIP pausado

El alcance de Teams queda pausado por decisión del usuario. Se conserva el código
existente; no se ejecutará automatización de Teams mientras esté pausado.

- Hay un esqueleto del MVP y 12 pruebas unitarias aprobadas.
- Se implementó el inicio manual en un perfil aislado de Playwright.
- Al reabrir el perfil, Teams vuelve al inicio de sesión; la causa no está confirmada.
- No se validaron los selectores contra un chat autorizado ni se enviaron mensajes.
- Antes de reanudar, hay que resolver el acceso admitido por la organización y
  verificar el flujo real sin envíos.

La nueva prioridad es organizar facturas electrónicas de una cuenta personal de
Hotmail y registrarlas en un archivo Excel existente. El piloto acordado usa Opera
aislado, XML/ZIP/PDF, todos los periodos, una nueva columna de número de factura y
aprobación por lote. Solo se moverán correos de facturas registradas o duplicados
confirmados, hacia carpetas existentes sugeridas para revisión.

El núcleo de extracción y registro se probó con archivos sintéticos; el Excel real
sigue intacto. La integración con Outlook y la revisión de lotes quedan pendientes
del inicio manual en la cuenta personal y la validación del DOM real.

## 1. Objetivo

Construir un agente ejecutado localmente que pueda:

1. Abrir Microsoft Teams Web en Microsoft Edge.
2. Supervisar chats autorizados.
3. Detectar mensajes nuevos.
4. Leer el contexto necesario.
5. Generar una propuesta de respuesta con un modelo local.
6. Mostrar la propuesta para revisión humana.
7. Permitir editar, aprobar o rechazar la respuesta.
8. Insertar o enviar la respuesta mediante automatización controlada de Edge.
9. Registrar cada acción en un archivo local con auditoría.

La fase inicial no responderá automáticamente sin aprobación humana.

## 2. Alcance inicial

### Incluido

- Microsoft Teams Web.
- Microsoft Edge.
- Windows 10/11.
- Supervisión de chats o canales seleccionados.
- Respuestas en borrador.
- Aprobación manual.
- Modelo local mediante Ollama.
- Almacenamiento local de configuración y logs.
- Modo de pausa y cierre inmediato.

### Excluido en el MVP

- Envío completamente autónomo sin aprobación.
- Responder chats desconocidos o conversaciones personales no autorizadas.
- Descargar archivos adjuntos.
- Modificar mensajes existentes.
- Acceder a credenciales, contraseñas o datos de autenticación.
- Ejecutar acciones distintas de Teams sin autorización expresa.
- Evadir políticas corporativas, MFA o controles de seguridad.

## 3. Arquitectura propuesta

```text
Teams Web en Edge
       |
       v
Servicio de automatización Playwright
       |
       +--> Captura de mensajes nuevos
       |
       +--> Filtro de chats permitidos
       |
       v
Motor de contexto local
       |
       v
Modelo local mediante Ollama
       |
       v
Interfaz de aprobación
       |
       v
Edición y envío mediante Playwright
       |
       v
Registro local y métricas
```

### Tecnologías recomendadas

- Python 3.12.
- Playwright para Python.
- Ollama para modelos locales.
- FastAPI para coordinación interna opcional.
- Interfaz inicial mediante consola, Tkinter o una pequeña aplicación web local.
- SQLite para cola de mensajes, configuración y auditoría.
- Modelo inicial recomendado: Qwen 2.5 o equivalente, ajustado al hardware disponible.

Se recomienda comenzar sin agentes autónomos de control visual. Playwright ofrece una automatización más estable y auditable que capturar la pantalla y simular pulsaciones.

## 4. Requisitos técnicos

- Windows 10/11.
- Microsoft Edge actualizado.
- Python 3.12.
- Node.js solo si se utiliza JavaScript para Playwright.
- Ollama instalado y configurado.
- RAM mínima orientativa: 16 GB; se recomiendan 32 GB o GPU compatible para modelos mayores.
- Perfil de Edge separado para el agente.
- Carpeta local dedicada para configuración, logs y datos temporales.
- Cuenta de Teams autorizada por la organización.

No se deben guardar contraseñas ni tokens en código o archivos de configuración.

## 5. Plan por fases

### Fase 1: Requisitos y seguridad

Duración orientativa: 2-3 días.

- Confirmar si Teams Web está permitido por la política corporativa.
- Documentar chats, equipos y horarios autorizados.
- Obtener autorización explícita de los responsables.
- Clasificar qué información puede procesarse localmente.
- Definir tono, lenguaje y restricciones de respuesta.
- Definir qué temas requieren escalamiento humano.

Entregables:

- Lista de chats permitidos.
- Política de uso.
- Lista de temas prohibidos o sensibles.
- Plantilla de aprobación.

### Fase 2: Automatización mínima de Teams Web

Duración orientativa: 3-5 días.

- Instalar Playwright y su navegador compatible con Edge.
- Crear perfil persistente de Edge.
- Iniciar sesión manualmente la primera vez.
- Abrir Teams Web.
- Identificar chats permitidos.
- Capturar mensajes nuevos.
- Insertar texto en el editor de Teams.
- Añadir un botón o comando para enviar únicamente tras confirmación.
- Implementar modo de pausa.

Entregables:

- Script reproducible de inicio de sesión.
- Script de lectura limitada.
- Script de inserción controlada.
- Configuración de chats autorizados.

### Fase 3: Integración del modelo local

Duración orientativa: 3-5 días.

- Instalar Ollama.
- Probar modelos locales y medir calidad/latencia.
- Crear prompt con:
  - Identidad del asistente.
  - Tono profesional.
  - Contexto permitido.
  - Prohibición de inventar hechos.
  - Solicitud de aclaración cuando falte información.
  - Reglas de confidencialidad.
- Limitar el contexto a mensajes recientes y documentos autorizados.
- Mostrar la propuesta sin enviarla.
- Permitir pedir una nueva propuesta con instrucciones adicionales.

Entregables:

- Configuración Ollama.
- Prompt de sistema y plantillas.
- Pruebas de respuestas.
- Métrica de calidad y latencia.

### Fase 4: Interfaz de aprobación

Duración orientativa: 2-4 días.

Crear una interfaz local que muestre:

- Nombre o identificador del chat.
- Mensaje recibido.
- Contexto resumido.
- Respuesta propuesta.
- Motivos de cualquier escalamiento.
- Botones Aprobar, Editar, Regenerar, Rechazar y Pausar.
- Confirmación antes de enviar.
- Indicador de confianza y último test realizado.

Entregables:

- MVP interactivo.
- Flujo de aprobación probado.
- Mecanismo de parada de emergencia.

### Fase 5: Auditoría y privacidad

Duración orientativa: 2-3 días.

- Guardar registro de:
  - Hora.
  - Chat autorizado.
  - Mensaje recibido resumido.
  - Respuesta generada.
  - Edición final.
  - Acción realizada.
  - Persona que aprobó.
- Evitar guardar mensajes innecesarios.
- Aplicar retención de registros.
- Cifrar datos sensibles si la organización lo exige.
- Impedir que el modelo acceda por sí mismo a disco o red.

Entregables:

- Formato de log.
- Política de retención.
- Procedimiento de borrado.
- Exportación básica para auditoría.

### Fase 6: Pruebas piloto

Duración orientativa: 1-2 semanas.

- Prueba con chat interno de prueba.
- Prueba con mensajes representativos.
- Validar cambios de Teams y Edge.
- Medir:
  - Tasa de respuestas útiles.
  - Porcentaje editado.
  - Tiempo de respuesta.
  - Errores de selección de chat.
  - Incidentes de privacidad.
- Ejecutar una semana con aprobación humana obligatoria.
- No ampliar la automatización hasta no superar los criterios.

Entregables:

- Informe de piloto.
- Lista de fallos.
- Ajustes de prompt y automatización.

### Fase 7: Evaluación de envío autónomo

Solo tras autorizar formalmente la automatización.

- Iniciar con una lista corta de conversaciones.
- Límite máximo de mensajes por hora.
- Horarios permitidos.
- Bloqueo de temas sensibles.
- Escalamiento cuando no haya contexto suficiente.
- Envío en segundo plano solo si cumple controles adicionales.
- Identificación clara cuando una respuesta sea asistida por IA, si la política lo exige.

La aprobación humana debe mantenerse como valor predeterminado.

## 6. Componentes sugeridos

```text
src/
  app/
    approval_ui.py
    main.py
  automation/
    edge_session.py
    teams_web.py
    selectors.py
  ai/
    ollama_client.py
    prompts.py
    context_builder.py
  storage/
    database.py
    audit_log.py
  config/
    settings.py
    allowed_chats.yaml
tests/
  test_context_builder.py
  test_allowed_chats.py
  test_teams_web.py
  test_approval_flow.py
README.md
```

## 7. Reglas de seguridad obligatorias

1. Lista cerrada de chats autorizados.
2. Confirmación humana para enviar.
3. Modo de pausa visible.
4. Bloqueo ante autenticación, MFA o captchas.
5. No solicitar ni almacenar contraseñas.
6. Perfil de Edge separado.
7. Sin acceso directo del modelo al terminal.
8. Sin ejecución de código generado por el modelo.
9. Sin envío automático si falta contexto.
10. Sin acceso a archivos ni red por parte del modelo.
11. Registro de cada aprobación y envío.
12. Detención ante errores inesperados.
13. Cumplimiento de políticas de Microsoft 365 y de la organización.

## 8. Pruebas y criterios de aceptación

### MVP mínimo

- Detecta un mensaje nuevo en un chat permitido.
- Genera una respuesta útil dentro de 15 segundos.
- No ingresa ni modifica texto fuera del editor autorizado.
- No envía sin aprobación.
- Permite edición completa de la respuesta.
- Permite pausar y detener el agente.
- Registra la aprobación y la acción.
- Reanuda correctamente una sesión ya iniciada en Edge.

### Antes de considerar envío automático

- Más del 95 % de respuestas útiles en el conjunto piloto.
- Menos del 1 % de errores de selección de chat.
- Cero credenciales almacenadas por el agente.
- Cero envíos en chats no autorizados.
- Procedimiento probado ante fallos y cambios de Teams.
- Aprobar riesgos y uso con responsables de la organización.

## 9. Plan inmediato de las próximas acciones

1. Verificar la versión de Windows y el hardware disponible.
2. Confirmar si Teams Web está permitido por la organización.
3. Instalar Python, Ollama y Edge.
4. Probar Ollama con un modelo local pequeño.
5. Crear un perfil persistente de Edge.
6. Abrir Teams Web manualmente en Playwright.
7. Crear una prueba con un chat interno de desarrollo.
8. Implementar captura de mensajes y generación de borradores.
9. Implementar interfaz de aprobación.
10. Ejecutar piloto.
