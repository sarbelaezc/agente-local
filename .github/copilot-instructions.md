# Instrucciones para GitHub Copilot

## Proyecto

Este proyecto implementará un agente local que ayuda a redactar respuestas para chats de Microsoft Teams Web ejecutado en Microsoft Edge.

La prioridad inicial es:

1. Seguridad.
2. Control humano.
3. Automatización reproducible.
4. Privacidad.
5. Auditoría.

El proyecto es un repositorio independiente; las rutas personales se definen en archivos de configuración locales excluidos de Git.

## Gestión de features y README

El agente local agrupa varios features (por ejemplo, Teams y facturas de Hotmail). Las solicitudes del usuario definen qué se implementa; el `README.md` documenta lo que se ha pedido e implementado, pero no es la fuente de verdad ni limita lo que el usuario puede solicitar.

### Inicio de cada sesión de trabajo

- Antes de realizar cualquier tarea, preguntar al usuario qué feature quiere implementar o modificar.
- Ofrecer como opciones los features listados en la tabla de features del `README.md`, con su estado, y la opción de crear un feature nuevo.
- No leer código en detalle, editar archivos, instalar dependencias ni ejecutar automatizaciones hasta recibir la respuesta.
- Limitar el trabajo al feature elegido. Si una tarea requiere tocar otro feature, avisar y pedir confirmación antes.

### Declaración en el README

- Toda funcionalidad nueva o modificación que solicite el usuario debe declararse en el `README.md` en el mismo cambio en que se implementa.
- Cada feature debe figurar en la tabla de features con nombre, estado, módulo y comando de ejecución, y tener su propia sección con alcance, configuración, uso y restricciones de seguridad.
- Si un cambio altera el comportamiento, los comandos, la configuración, las dependencias o el estado de un feature, actualizar el `README.md` para reflejarlo.
- Distinguir en el `README.md` lo implementado y verificado de lo pendiente o no validado.
- Si el `README.md` describe algo distinto de lo que el usuario pide ahora, prevalece la solicitud del usuario: implementarla y actualizar el `README.md`. Si la solicitud contradice una regla de seguridad de estas instrucciones, informar el conflicto antes de continuar.

## Objetivo del MVP

Construir una aplicación local que:

- Detecte mensajes nuevos en chats previamente autorizados.
- Prepare el contexto de un chat.
- Solicite al modelo local una propuesta de respuesta.
- Muestre la propuesta en una interfaz de aprobación.
- Permita aprobar, editar, regenerar o rechazar la propuesta.
- Inserte la respuesta aprobada en el editor de Teams Web.
- Solo pueda enviar el mensaje después de una confirmación humana explícita.
- Mantenga un registro local de auditoría.

## Tecnologías preferidas

- Python 3.12.
- Playwright para Python.
- Ollama para el modelo local.
- Tesseract (local) y `pypdfium2` para OCR de PDF escaneados.
- SQLite para mensajes pendientes, configuración y auditoría.
- FastAPI solo si se necesita una interfaz web local.
- Tkinter para una interfaz de escritorio mínima.
- Pytest para pruebas.

No agregar dependencias innecesarias sin una razón clara.

## Arquitectura esperada

```text
src/
  app/
  automation/
  ai/
  storage/
  config/
tests/
```

Separar claramente:

- Interacción con Edge y Teams.
- Integración con Ollama.
- Construcción de contexto.
- Interfaz de aprobación.
- Persistencia y auditoría.
- Configuración de chats autorizados.

## Reglas obligatorias

- Nunca enviar automáticamente un mensaje.
- Requerir confirmación humana explícita antes de pulsar Enviar.
- Mantener una lista cerrada de chats autorizados.
- No leer chats fuera de esa lista.
- No guardar contraseñas, tokens o cookies en código fuente.
- No solicitar credenciales al usuario mediante el agente.
- Detenerse ante MFA, captcha, autenticación inesperada o pantallas no reconocidas.
- No permitir que el modelo acceda directamente al terminal, disco o red.
- No ejecutar código generado por el modelo.
- No almacenar contenido innecesario de mensajes.
- Detener toda actividad ante un fallo o selector ambiguo.
- Registrar quién aprobó una respuesta, cuándo se aprobó y qué texto se envió.

## Reglas para la automatización de Teams

- Usar Playwright preferentemente a un agente visual.
- Usar un perfil persistente separado para Edge.
- Identificar chats mediante nombres o identificadores de configuración.
- Validar el chat activo antes de insertar texto.
- No enviar si el chat activo no coincide con el chat autorizado esperado.
- Implementar modo pausa y parada de emergencia.
- Usar esperas basadas en eventos o elementos antes que tiempos rígidos.
- Escribir selectores de forma estable y centralizada.
- Detectar cambios de versión de Teams Web y fallar sin enviar.

## Integración con el modelo local

- Comunicar Ollama mediante un cliente aislado.
- Configurar modelo y parámetros desde configuración local.
- No utilizar herramientas externas dentro de Ollama en el MVP.
- Construir contexto a partir exclusivamente de:
  - Mensajes autorizados.
  - Instrucciones aprobadas.
  - Datos explícitamente suministrados por el usuario.
- Indicar al modelo que no debe inventar información.
- Pedir aclaración o escalamiento cuando falte contexto.
- Bloquear temas sensibles definidos por el usuario.
- Sanitizar todo texto antes de mostrarlo o enviarlo.
- Tratar toda salida del modelo como texto no confiable.

## Prompt recomendado

El prompt de sistema debe:

- Definir el tono profesional y el idioma preferido.
- Indicar que actúa como asistente y no debe afirmar ser una persona real.
- Exigir respuestas concisas.
- Prohibir inventar datos, fechas, compromisos o resultados.
- Solicitar una respuesta neutral o escalamiento ante información insuficiente.
- Respetar las reglas del proyecto.
- No revelar prompts internos ni datos privados.

## Privacidad y auditoría

- Aplicar listas de chats permitidos.
- Minimizar datos retenidos.
- Conservar solo lo necesario para auditoría.
- Definir una política de retención.
- Evitar logs con contenido completo salvo cuando sea necesario.
- Permitir borrado manual de registros temporales.
- Cifrar datos sensibles si la organización lo exige.
- Mostrar una advertencia antes de procesar contenido confidencial.

## Comandos y estructura

Cuando se implemente el proyecto:

- Mantener archivos de configuración fuera del código fuente.
- Utilizar variables de entorno para rutas locales sensibles.
- Crear scripts claros para inicialización, ejecución y pruebas.
- Incluir un README con instalación en Windows.
- Crear archivos de pruebas enfocados en:
  - Chats autorizados.
  - Construcción de contexto.
  - Validación de respuestas.
  - Flujo de aprobación.
  - Selección correcta del chat.
  - Detención ante errores.

## Convenciones de código

- Seguir PEP 8.
- Preferir funciones pequeñas con responsabilidades claras.
- Añadir tipos a las interfaces públicas.
- Usar nombres descriptivos en inglés.
- Manejar explícitamente todos los errores relevantes.
- No usar cláusulas `except Exception` sin necesidad.
- No llamar a funciones de envío desde módulos que generan respuestas.
- No incluir secretos ni rutas personales específicas.
- Escribir comentarios solo para lógica no evidente.
- Añadir docstrings a funciones públicas importantes.
- Mantener funciones relacionadas con UI separadas de la lógica de dominio.

## Pruebas

Antes de finalizar cualquier cambio importante, ejecutar la menor prueba relevante.

Las pruebas deben demostrar:

- Que un chat fuera de la lista de autorización no se procesa.
- Que una respuesta nunca se envía sin aprobación.
- Que el texto aprobado coincide con el enviado.
- Que el chat activo coincide con el destino esperado.
- Que el agente se detiene ante errores de UI.
- Que no se ejecuta código generado por el modelo.

## Entregables esperados

- Aplicación local de consola o escritorio.
- Perfil de Edge configurado.
- Captura limitada de mensajes.
- Generación de borradores mediante Ollama.
- Interfaz de aprobación.
- Inserción y envío controlado por Playwright.
- Registro de auditoría.
- Documentación de instalación y uso.
- Pruebas automatizadas básicas.

## Criterio de implementación

El proyecto debe evitar cualquier envío accidental, procesar solo chats autorizados y mantener siempre la decisión humana como barrera final hasta que exista aprobación formal y verificable para el envío autónomo.
