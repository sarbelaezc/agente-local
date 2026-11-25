# Informes de Invesbot en el cierre mensual

Fecha: 2026-11-25. Funcionalidad: cierre mensual. Los ejemplos usan cifras ficticias.

## Intención

Los informes mensuales de los planes de Invesbot no llegan al correo: el usuario los
descarga del banco. Quiere integrarlos a mano cuando lleguen para:

1. Confirmar los aportes al plan contra lo registrado en el cierre.
2. Ver cada plan en la gráfica de inversiones del año, con saldo, aportes, retiros y
   rendimientos (incluidas las pérdidas), como los extractos de Protección.

El informe lo genera el banco y prima sobre lo registrado.

## Objetivos

- Leer los PDF de `invesbot/<titular>/` en la carpeta de datos del cierre en cada `scan`,
  `import` y cierre programado (sin `-ReadOnly`), sin Ollama.
- Guardar por plan y mes el saldo y las cifras del período, y mostrarlos en Inversiones
  (pestaña Mes y gráfica del año).
- Comparar los aportes acumulados del informe con los registrados y, cuando falten,
  devolver a Inversión los aportes del plan marcados «No cuenta» en ese mes.
- Avisar en la terminal y en el panel de la diferencia que no se puede resolver sola.

## No objetivos

- Crear movimientos nuevos a partir del informe: no trae fechas de aportes.
- Leer informes desde el correo o subirlos desde el panel.
- Cambiar cómo se leen los traslados en los extractos de ahorros o las Fiducuentas.

## Antiobjetivos

- Contar un aporte dos veces (informe más avisos).
- Volver a tocar una decisión que una persona corrigió después de la corrección automática.
- Importar algo de un informe que no se lee con exactitud.

## Restricciones

- Reglas de `AGENTS.md`: datos solo en `.local` (o `AGENTE_LOCAL_DATA`), nada de
  contenido en los registros, el modelo no interviene, toda decisión auditada.
- Un informe ilegible, sin las cifras esperadas o cuyas cifras no suman detiene todo
  sin guardar nada, como un extracto.
- Repetir no cambia nada.
- Ningún informe real ni cifra real entra al repositorio: las pruebas usan texto sintético.

## Enfoques considerados

1. **Carpeta local leída en cada `import`** (elegido por el usuario). Como el Excel de
   créditos: no hay que recordar un paso aparte.
2. Comando aparte `invesbot`: más explícito, pero es otro paso que recordar.
3. Subir los PDF desde el panel: obliga a recibir y guardar archivos por la red.

## Lo que decidió el usuario

- Carpetas `invesbot/santiago` y `invesbot/manuela` (ya creadas); el titular es el de la
  carpeta.
- Sin contraseña; si algún día la tienen, es la del titular en `statements.json`.
- Si al informe le falta un aporte en un mes y en ese mes hay un aporte del plan
  marcado «No cuenta», pasa **solo** a Inversión: el informe del banco tiene la verdad.
- Los traslados «TRASLADO A FONDO DE INVERSION» del ahorro pueden ir a Invesbot o a una
  de varias Fiducuentas (cuyos extractos sí llegan al correo); el texto no basta para
  saber a cuál.

## Formato del informe (verificado con informes reales de varios meses)

PDF de 6 páginas, con texto. Página 1: «Invesbot | Reporte plan inversión», nombre del
plan y «<Mes> de <año>». El banco escribe «Sptiembre». Página 2, de la que salen las
cifras:

| Rótulo | Uso |
|---|---|
| `Reporte plan inversión - <Mes> de <año>` | Período: el mes completo |
| `Fecha de inicio` (`Ene 13 de 2026`) | Inicio del plan |
| `TOTAL DEL PLAN SALDO AL CIERRE DEL MES $ …` | Saldo al cierre |
| `MONTO DE APERTURA $ …` | Primer aporte |
| `APORTES ADICIONALES $ …` y `N aportes` | Aportes acumulados desde el inicio |
| `RETIROS REALIZADOS $ …` (a veces sin espacio) | Retiros acumulados |
| `RENDIMIENTOS AL CIERRE DEL MES $ …` (puede ser negativo) | Rendimiento acumulado |

Validación: apertura + aportes adicionales − retiros + rendimientos acumulados = saldo,
exacto al centavo. Se cumple en todos los informes revisados.

No se usan «Aportes último mes» ni «Rendimientos último mes»: contradicen los
acumulados (los aportes del mes salen en cero aunque el acumulado sube cada mes).

## Diseño

### Lectura

- Archivos `*.pdf` de `invesbot/santiago` y `invesbot/manuela`, de hasta 10 MB. Otra
  carpeta o titular no se lee. Si ninguna carpeta tiene PDF, no pasa nada.
- Se extrae el texto en memoria; se valida lo anterior. El período sale del texto, no
  del nombre del archivo.
- El plan es el nombre de la página 1. Dos informes del mismo titular, plan y mes con
  cifras distintas detienen todo; iguales cuentan una vez.

### Cifras guardadas

Cada informe es una fila de `statement_checks`: producto `INVESBOT`, sufijo = nombre del
plan, período = mes del informe, `balance_minor` = saldo, titular = carpeta. Las cifras
del período salen de la diferencia con el informe anterior del mismo plan (con huecos de
meses, la diferencia cubre el hueco); sin informe anterior, desde el inicio del plan con
saldo 0:

- `opening_minor`: saldo del informe anterior (0 sin anterior).
- `contributions_minor`: aumento de apertura + aportes adicionales.
- `withdrawals_minor`: aumento de retiros.
- `returns_minor`: saldo − apertura del período − aportes + retiros; puede ser negativo.

Al guardar un informe más antiguo después, se recalculan las cifras del siguiente.

### Aportes del plan registrados

Movimientos del titular desde el inicio del plan:

- **Del plan por nombre:** comercio «Invesbot <plan>», o «Invesbot» sin plan cuando el
  titular tiene un solo plan con informes.
- **Traslados del ahorro:** comercio «TRASLADO A FONDO DE INVERSION». Cuentan para el
  plan solo si caben en lo que al informe le falta ese mes (ver abajo); así un traslado
  grande a una Fiducuenta queda fuera.

Registrado = decididos como Inversión menos los del plan decididos como Retiro de
inversión.

### Comparación y corrección (mes a mes, en orden)

Para cada informe del plan, de más antiguo a más nuevo:

1. Esperado = apertura + aportes adicionales − retiros del informe (acumulado al cierre
   del mes). Registrado = acumulado hasta el último día del mes.
2. Falta = esperado − registrado. Si es positivo, primero se cuentan los traslados del
   ahorro de ese mes, del más antiguo al más nuevo, mientras quepan.
3. Si sigue faltando, se buscan aportes del plan por nombre del mismo mes marcados
   «No cuenta»:
   - Se pasan a Inversión, del más antiguo al más nuevo, mientras quepan en lo que falta.
   - Cada cambio se audita (`invesbot-restore`) con el informe que lo justificó, y el
     movimiento lleva una nota que lo dice.
   - Un movimiento corregido una vez no se vuelve a tocar, aunque la persona lo devuelva a
     «No cuenta».
4. Lo que siga faltando, o sobrando, es una diferencia: no se crea ningún movimiento. Se
   avisa solo la del informe más reciente de cada plan, porque es acumulada, en la
   terminal y en el panel («El informe cuenta X en aportes; hay Y registrados»).
   Decisión del implementador, revisable.

Ejemplo ficticio con aportes mensuales de 100: el informe de julio espera 700 y hay 600
registrados; un aporte del plan del 1 de julio marcado «No cuenta» pasa a Inversión. En
agosto espera 800 y hay 700: un traslado del ahorro de 100 de ese mes cuenta para el plan
y otro «No cuenta» de agosto no se toca. Un aporte faltante sin «No cuenta» en su mes se
avisa.

### Momento

Dentro de los complementos del recorrido (`run_complements`): después de conciliar los
extractos (que pueden importar traslados) y antes de marcar la cobertura. Un fallo
detiene todo, como el resto.

### Panel

- `INVESBOT` es un producto de Inversiones: aparece en la pestaña Mes y en la gráfica y
  la lista del año, con el nombre «Invesbot <plan>» (sin asterisco).
- Muestra saldo, aportes, retiros y rendimientos del período como Protección, y la
  diferencia de aportes cuando la hay.
- En **Hogar** lleva la etiqueta del titular.

### Documentación

README (sección del cierre) y `docs/cierre-mensual.md`: carpeta, qué se lee, la
corrección automática y el aviso. `docs/configuracion.md`: las carpetas. Sin cifras ni
nombres de planes reales.

## Pruebas

Con informes sintéticos (texto, sin PDF reales en el repositorio):

- Lectura de las cifras, «Sptiembre», rendimientos negativos, retiros sin espacio.
- Un informe que no suma o sin un rótulo detiene sin guardar nada.
- Cifras del período con y sin informe anterior y con un mes faltante.
- Corrección: el ejemplo de arriba, un traslado que no cabe, un movimiento ya corregido
  que la persona revirtió, y repetir sin cambios.
- Nunca se crean movimientos ni se corrige otra cosa que aportes del plan «No cuenta».
- El panel muestra el plan en Inversiones (Chromium).

Validación con datos reales: en solo lectura sobre una copia de la base, antes de
importar.

## Decisiones del implementador

- Texto exacto de la nota y del aviso (en español, con mayúscula inicial).
- Dónde guardar la diferencia de aportes para el panel (columna nueva o cálculo al
  armar el estado).
- Biblioteca de PDF: la que ya usa el proyecto.
