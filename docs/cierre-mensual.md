# Cierre mensual de gastos

Lee los avisos del banco de Santiago (Outlook) y de Manuela (Gmail), propone cada
movimiento con Ollama local y te deja decidir en un panel cuánto fue gasto, ingreso
o inversión. Nada cuenta hasta que una persona lo decide.

Para instalar y configurar, ver [configuracion.md](configuracion.md).

- [Cómo funciona](#cómo-funciona)
- [Primer uso](#primer-uso)
- Tareas:
  - [Importar avisos](#importar-avisos)
  - [Decidir movimientos](#decidir-movimientos)
  - [Reglas por comercio](#reglas-por-comercio)
  - [Compras en dólares](#compras-en-dólares)
  - [Ingresos](#ingresos)
  - [Inversiones y retiros](#inversiones-y-retiros)
  - [Avisos sin extraer](#avisos-sin-extraer)
  - [Movimientos de Manuela por archivo](#movimientos-de-manuela-por-archivo)
  - [Cuentas y transferencias entre titulares](#cuentas-y-transferencias-entre-titulares)
  - [Conciliar extractos](#conciliar-extractos)
  - [Conciliación automática del mes](#conciliación-automática-del-mes)
  - [Créditos y nómina](#créditos-y-nómina)
  - [Unir categorías](#unir-categorías)
  - [Cierre y tarea programada](#cierre-y-tarea-programada)
  - [Probar con un solo aviso](#probar-con-un-solo-aviso)
- Referencia:
  - [El panel](#el-panel)
  - [Lectura de los buzones](#lectura-de-los-buzones)
  - [Frases del banco leídas sin Ollama](#frases-del-banco-leídas-sin-ollama)
  - [Validación de lo que propone Ollama](#validación-de-lo-que-propone-ollama)
  - [Privacidad y límites](#privacidad-y-límites)
- [Estado](#estado)

## Cómo funciona

1. **Lectura.** El agente recorre los buzones en solo lectura y abre únicamente
   los mensajes de los remitentes exactos que aprobaste en el paso 0. Valida el
   origen de cada mensaje y nunca guarda su cuerpo.
2. **Propuesta.** Las frases fijas del banco (pagos de tarjeta, transferencias…)
   se leen con reglas deterministas, sin el modelo. El resto lo interpreta Ollama
   local, y cada dato que propone (comercio, monto) debe aparecer literalmente en
   el aviso; si no, el aviso va a una cola de revisión en vez de inventarse algo.
3. **Decisión.** Todo movimiento entra **pendiente**. Tú lo decides en el panel
   (o lo decide una regla que tú escribiste): gasto personal, familiar, ingreso,
   inversión, abono de tercero o no cuenta. Cada decisión queda auditada y una
   nueva importación nunca la reemplaza.
4. **Totales.** El panel suma solo lo decidido, con enteros en centésimas, nunca
   con aritmética del modelo.

**Titulares.** Cada movimiento tiene un titular: Santiago o Manuela. El panel
muestra a cada uno por separado o el **Hogar**, que los suma.

| Tema | Regla |
|---|---|
| Gasto | Personal + familiar − abonos de terceros. El balance es ingresos confirmados − ese gasto |
| No es gasto | Préstamos (salvo cuotas de crédito decididas), compras rechazadas, promociones, códigos, pagos de tarjeta e inversiones. Transferencias propias o a cuentas excluidas solo admiten Familiar o No cuenta |
| «Familiar» en Hogar | Un solo rubro del hogar; el detalle muestra el titular de cada movimiento |
| Entre Santiago y Manuela | Conservan su decisión en la vista de cada titular y no cuentan en Hogar, para no contar dos veces el mismo dinero |
| Retiro en cajero | Gasto personal para ambos titulares. Por eso el gasto personal de Manuela queda por encima del de su panel anterior en los meses con retiros, exactamente por el monto retirado |
| Abono de tercero a una tarjeta | Resta del gasto. Solo en pagos a tarjeta. En Santiago solo resta lo que se marque explícitamente |

**Monedas y fechas.** No se mezclan monedas: un cierre en COP admite avisos en USD
solo con una tasa que pones tú. Las fechas con zona horaria se convierten a la
zona del paso 0; nunca se supone UTC. Si el aviso no tiene fecha de compra, se usa
la fecha local del aviso y la propuesta lo indica.

**Cobertura.** Cada mes importado empieza **parcial**: contar correos no demuestra
que estén todos. Se confirma de dos formas:

- **Conciliado** (automático): el agente lo marca cuando los extractos de los dos
  titulares cuadran. Ver [Conciliación automática del mes](#conciliación-automática-del-mes).
- **Revisado** (manual): lo marcas tú en el panel, con una nota de conciliación.

Un movimiento nuevo en un mes conciliado o revisado lo devuelve a parcial.

## Primer uso

Con la [configuración](configuracion.md#4-cierre-mensual-runtimejson-y-paso-0)
completa (paso 0, `runtime.json`, sesión y recorrido validado):

1. Aprueba en el panel las direcciones exactas de los avisos de tu banco.
2. Importa: `.\scripts\agente-local.ps1 import`. La primera vez lee desde el 1 de
   enero del año en curso; después, solo lo nuevo.
3. En la pestaña **Decisión**, decide los pendientes.
4. Revisa el mes en la pestaña **Mes** y compáralo con tus extractos.

## Importar avisos

```powershell
.\scripts\agente-local.ps1 import                                   # solo lo nuevo
.\scripts\agente-local.ps1 import -Start 2026-01-01 -End 2026-11-01 # rango completo
```

`import` comprueba la configuración y el modelo, inicia Ollama si hace falta, lee
los buzones y al terminar abre el panel en `http://127.0.0.1:8766` (o reutiliza el
que esté abierto). Mantén abierta la terminal del panel mientras revisas. Si la
lectura falló, el panel se abre igual pero se conserva el código de error:
abrirlo no significa que el recorrido terminara bien. `scan` hace lo mismo sin
abrir el panel.

- **Sin fechas (incremental).** Lee solo lo que cambió desde la ejecución
  anterior, incluido el mes en curso. Ver
  [Lectura incremental](#lectura-incremental).
- **Con fechas (recorrido completo).** `-End` es exclusivo: usa `-End 2026-11-03`
  para incluir el 2 de noviembre. Este modo comprueba además que cada carpeta
  coincida con su total, así que es el que demuestra la cobertura de un rango.
- **Repetir es seguro.** Los mensajes ya importados se omiten por `Message-ID`,
  aunque aparezcan en otra carpeta, y las decisiones no se sobrescriben.
- **Reanudar.** Si una ejecución con fechas se interrumpe, repetirla con el mismo
  rango no vuelve a abrir los avisos ya completados. El progreso se guarda como
  huellas, sin asuntos ni cuerpos, y se borra al terminar.
- **Progreso.** En una terminal interactiva se ve una barra por carpeta
  (`[#####......] 36% | dinero / bancolombia | 2026-08-02 | 2 avisos`). No aparece
  en la tarea programada ni al redirigir la salida, y nunca muestra contenido.
- Al terminar de leer los avisos, cada `scan`, `import` o `scheduled` (sin
  `-ReadOnly`) concilia los extractos si existe `statements.json` e importa los
  registros de créditos si existe `credit_ledgers.json`. Un fallo en cualquier
  paso detiene todo.

## Decidir movimientos

La pestaña **Decisión** agrupa los pendientes por **destino**: titular más
destino del pago (o las palabras del comercio, si el aviso no trae destino), con
los más antiguos primero: va primero el destino cuyo movimiento más antiguo es más
viejo, y a igual fecha el de mayor total, para cerrar antes los meses que bloquean
la conciliación. Cada tarjeta muestra el número de movimientos, el rango de fechas,
el total y una pista, con botones que deciden todos sus movimientos a la vez.
«Decisiones anteriores» va del más reciente al más antiguo.

- **Progreso.** Arriba, «Por decidir» muestra los destinos, los movimientos y el
  importe pendientes de la vista, y un botón por titular con sus movimientos por
  decidir («Santiago: 16», «Manuela: 68») que abre la vista de ese titular.
- **Siguiente tarjeta.** Al decidir un destino o un movimiento, el foco pasa a la
  tarjeta que ocupa ahora su lugar, para seguir con el teclado o sin desplazarte.
- **Todo o nada.** Si un movimiento no pasa la validación (por ejemplo, una compra
  en USD sin tasa), no cambia ninguno.
- **Pagos futuros.** «Aplicar también a pagos futuros a este destino» crea una
  regla de destino, que se puede borrar en «Reglas por destino». Sin esa casilla
  no se crea ninguna regla.
- **Categoría del destino.** Una tarjeta con varios movimientos que admiten Gasto
  personal o Familiar (compras, transferencias como un traslado a Nequi, cuotas
  de crédito o movimientos sin tipo) tiene el selector «Categoría para los N
  movimientos (Gasto personal o Familiar)», que incluye «Retiro en efectivo» para
  traslados que sacaste en efectivo. Los traslados a inversiones no lo
  tienen: Inversión y Retiro de inversión asignan su propia categoría. Al decidir
  Gasto personal o Familiar, todos toman esa categoría, con la misma validación
  todo o nada y una auditoría por movimiento; las demás decisiones no la usan.
  Muestra la sugerencia común si la pediste, si no la categoría que ya comparten,
  y si no «Mantener la de cada movimiento», que no cambia ninguna. También está
  en «Decisiones anteriores» para recategorizar un destino ya decidido.
- **Uno por uno.** Debajo de cada tarjeta se puede decidir cada movimiento, con
  su categoría y su tasa USD. Decidir un movimiento confirma su categoría.
- **Corregir.** «Decisiones anteriores» muestra el historial y permite cambiar
  una decisión. Para volver un movimiento a gasto, elige primero una categoría de
  consumo.
- La pestaña muestra todos los meses de la moneda elegida (en COP también los
  movimientos en USD, para ponerles tasa), en tandas de hasta 50.

## Reglas por comercio

En **Decisión**, «Reglas por comercio» evita repetir la misma decisión: «si el
comercio contiene `PRIMAX`, es Transporte y Familiar». Las escribes tú; Ollama no
las aplica ni decide nada por su cuenta.

- Coinciden por palabras completas, sin distinguir mayúsculas ni signos: `UBER`
  coincide con `UBER*RIDES`, pero `IPS` no coincide con `TIPS`. Si coinciden
  varias, gana la más larga.
- Solo deciden **compras** pendientes como Gasto personal o Familiar con una
  categoría configurada. También admiten el tipo **Ingreso** para dinero recibido
  («si quien paga contiene `EMPRESA`, es Nómina»).
- Nunca deciden transferencias, inversiones, cuentas excluidas, compras en USD sin
  tasa, movimientos importados de un extracto ni posibles duplicados.
- Se aplican al importar y al guardar la regla. Nunca cambian una decisión ya
  tomada; cada decisión automática indica su regla y se puede corregir. Borrar una
  regla no deshace lo que ya decidió.

### Sugerencias de reglas

El botón **Sugerir reglas** (en «Reglas por comercio») propone una categoría para
los comercios de compras pendientes de avisos que ninguna regla cubre (hasta 25,
los más frecuentes primero). Usa el mismo historial y los mismos ejemplos que las
[sugerencias de categoría](#sugerencias-de-categoría-para-pendientes). Requiere un
`runtime.json` válido y Ollama activo.

- Cada sugerencia aparece con el comercio (editable), la categoría y el tipo
  (Gasto personal o Familiar). **Aprobar regla** la guarda como una regla normal,
  con su auditoría; **Descartar** la quita. Ollama no elige Personal o Familiar,
  y mientras no apruebes no se guarda ni se decide nada.
- Las sugerencias no se conservan: al recargar el panel hay que pedirlas de nuevo.
- Solo cubren compras de avisos de correo. Si no queda ninguna sin regla, no hay
  nada que sugerir: las compras de extractos nunca las decide una regla. Para esas
  usa las sugerencias de categoría.

### Sugerencias de categoría para pendientes

Las compras que solo muestra un extracto llegan ya con una **categoría sugerida**:
al registrar extractos (`reconcile-statements`, no con `-ReadOnly`) se calcula como
se explica abajo y la razón del movimiento dice su origen («Categoría sugerida por
tus decisiones anteriores de este comercio; confírmala al decidir»). No decide nada:
siguen pendientes. Si Ollama no responde, la importación sigue con solo tus reglas
y decisiones anteriores, y lo demás queda en «Otros gastos»; el comando lo avisa.

En **Decisión**, el botón **Volver a sugerir categorías** recalcula la sugerencia
para todas las compras pendientes, por ejemplo después de crear reglas o decidir
más movimientos, o para las que se importaron antes con «Otros gastos». Solo
preselecciona, sin guardar. Requiere un `runtime.json` válido y Ollama activo.

- Primero, sin Ollama: si una de tus reglas por comercio coincide, se sugiere su
  categoría; si no, y ya decidiste ese mismo comercio como Gasto personal o
  Familiar siempre con la misma categoría, se sugiere esa. Un comercio decidido
  con categorías distintas no se sugiere así.
- Los demás van a Ollama (hasta 60, los más frecuentes) con tu lista de
  categorías y, como ejemplos, hasta 5 comercios ya decididos por cada uno que
  comparten palabras con él (los números de sucursal no cuentan) o reglas tuyas
  parecidas, con un máximo de 150 ejemplos por consulta. Ollama no recibe todo tu
  historial ni aprende de él: cada consulta empieza de cero.
- Ollama solo ve nombres de comercio y categorías, sin importes, fechas ni
  cuentas. Su respuesta se trata como texto no confiable: se descartan los
  comercios no preguntados y las categorías fuera de tu lista. No incluye ingresos.
- La sugerencia solo **preselecciona** la categoría, con una nota que dice si
  viene de tus reglas, de tus decisiones anteriores o de Ollama: en cada
  movimiento, y en el selector de categoría de la tarjeta del destino cuando
  todos sus movimientos tienen la misma sugerencia (ver
  [Decidir movimientos](#decidir-movimientos)).
- Nada se decide hasta que pulsas una decisión. Las sugerencias se guardan aparte
  de los movimientos (tabla `category_suggestions`, solo categoría y origen), así
  que siguen ahí al recargar el panel; dejan de mostrarse cuando decides el
  movimiento. «Volver a sugerir categorías» las reemplaza todas.

## Compras en dólares

En un cierre en COP, un aviso que dice `USD` o `US$` se guarda en dólares; el
nombre del comercio (por ejemplo, AMAZON MKTPLACE) nunca decide la moneda. Un
aviso con marcas de COP y USD a la vez se detiene por ambiguo.

1. En **Decisión** (con COP seleccionado también aparecen los USD), escribe la
   **tasa estimada COP por USD** de la fecha del movimiento, por ejemplo `4200,50`
   (punto o coma decimal, sin separador de miles, hasta seis decimales).
2. Confirma la decisión. El importe en USD queda intacto y se guarda su
   equivalente en COP, que es el que suman los totales. Sin tasa, el movimiento
   no suma a COP; «No cuenta» lo excluye sin tasa.

La tarjeta de un destino con varias compras muestra su total en la moneda de cada
movimiento (por ejemplo `US$ 35,00`), nunca 0,00 COP por falta de tasa. Con COP
seleccionado, añade el estimado en COP solo si todos sus movimientos en dólares
tienen tasa.

La tasa la pones tú: no se consulta una tasa externa ni la propone el modelo. Si
un aviso en USD quedó guardado como COP, comprueba el correo original, cambia
**Moneda original del aviso** a USD y pon la tasa al decidir.

Tras actualizar el código, reinicia el servidor del panel: recargar la página no
basta, y el panel bloquea las decisiones si detecta un servidor anterior.

## Ingresos

El dinero recibido (nómina, transferencias recibidas, consignaciones) entra como
ingreso **pendiente**. En **Decisión** se confirma con **Ingreso** o se descarta
con **No cuenta** (por ejemplo, un traslado entre cuentas propias o un reembolso).

- El tipo (`Nómina`, `Transferencia recibida`, `Consignación`, `Otros ingresos`)
  sale de las palabras del aviso, no del modelo, y se puede cambiar. Un ingreso
  nunca puede marcarse como gasto.
- Un movimiento clasificado como transferencia o desconocido también ofrece
  **Ingreso**, por si llegó mal clasificado. Una compra no.
- Un retiro del propio fondo o la devolución de una compra no son ingresos:
  llegan como desconocidos para que decidas.

**Recuperar avisos que no dejaron movimientos.** Un aviso ya procesado no se
vuelve a leer. Para releer solo los que no dejaron ningún movimiento (por ejemplo,
ingresos que versiones anteriores descartaban):

```powershell
.\scripts\agente-local.ps1 scan -Start 2026-01-01 -End 2026-12-01 -ReprocessEmpty
```

No toca los avisos que ya tienen movimientos ni sus decisiones. También reemplaza
los pagos de tarjeta que el modelo hubiera adivinado por el pago leído del aviso.

## Inversiones y retiros

| Decisión | Efecto |
|---|---|
| **Inversión** (botón) | Aporte a un fondo propio. Suma en un total aparte de inversiones; no es gasto |
| **Retiro de inversión** (botón) | Resta de las inversiones netas del mes y del año (puede dar un neto negativo). No es gasto ni ingreso |
| **Retiro en efectivo** (categoría) | Elige la categoría «Retiro en efectivo» y confirma Gasto personal o Familiar. Cuenta como gasto; no registres de nuevo lo que pagaste con ese efectivo |

El total de inversiones es aportes menos retiros confirmados: no es el saldo del
fondo, ni rendimientos, ni patrimonio (el saldo real viene de los
[extractos](#conciliar-extractos)). Ingresos, préstamos, pagos de tarjeta y
cuentas excluidas no se pueden sumar a inversiones.

## Avisos sin extraer

Cuando lo que propone Ollama no aparece literalmente en el aviso, se le pide
**una sola corrección**. Si vuelve a fallar, el aviso no se importa ni se marca
como procesado: queda en «Avisos sin extraer» (en **Decisión**, en un bloque aparte
de los movimientos por decidir) con remitente, asunto, fecha y hora, carpeta y el
motivo, y el recorrido sigue con los demás.

El agente **no guarda** la propuesta rechazada ni el texto del correo: la propuesta
no es confiable (no coincidió con el aviso) y el cuerpo nunca se almacena. Por eso
la tarjeta no muestra monto ni comercio. Para decidir, abre el aviso en Outlook por
su fecha, hora y remitente: si trae un movimiento, déjalo en la cola (cada lectura
lo reintenta); si no trae ninguno, descártalo.

- El campo rechazado indica el motivo: `merchant` (comercio), `amount_text`
  (monto), `events` (el aviso dice que salió dinero y no se extrajo nada) o
  `correction` (la corrección cambió otros datos o quitó movimientos).
- Mientras haya avisos en esa cola, el panel advierte que faltan movimientos y no
  deja marcar el mes como revisado.
- Cada ejecución siguiente los reintenta.
- **Descartar aviso** lo saca de la cola cuando no trae nada que importar; queda
  auditado y `scan -ReprocessEmpty` lo vuelve a leer.
- Un aviso que dice «Transferiste/Compraste/Retiraste/Enviaste» con un importe y
  del que el modelo no saca nada también va a la cola, en vez de perderse.

Los fallos de sesión, de Graph o de Gmail, y cualquier otra validación, sí
detienen la ejecución.

## Movimientos de Manuela por archivo

Los movimientos de Manuela hasta su último día exportado de su panel anterior
entran por archivo. Desde ese día, si su Gmail está configurado, el agente lo lee
directamente.

```powershell
.\scripts\agente-local.ps1 import-manuela
```

Lee de `.local\ImportManuela\` los archivos `movimientos_manuela.normalizado.json`,
`decisiones_manuela.json` y, si existe, `paridad_totales_manuela.json`.

- Valida todo antes de escribir. Si un registro falla, no importa nada e indica
  solo su número, nunca su contenido. Escribe todo en una transacción.
- Es idempotente: lo que ya existe (`manuela:<id>`) se omite.
- Lo que Manuela ya había decidido entra decidido y auditado como
  `decision-manuela`. Los pagos a destinos se deciden con su decisión por destino,
  que se guarda como regla; si una persona ya cambió esa regla, no se sobrescribe.
- Las compras en moneda extranjera entran en COP tal como las convirtió Manuela
  (4.000 COP/USD), con la nota «aprox.».
- Las categorías dudosas quedan como «Categoría por confirmar» hasta que alguien
  decida ese movimiento.
- Al terminar compara los totales de cada mes con el archivo de paridad y se
  detiene con error si alguno no coincide (descontando la diferencia aprobada del
  cajero).
- **Día de corte.** El Gmail de Manuela empieza en el último día del archivo, ese
  día incluido. Un movimiento del buzón con el mismo día, importe y moneda que uno
  del archivo entra **pendiente** con la nota «Posible duplicado…»; ninguna regla
  lo decide. Si es el mismo dinero, elige «No cuenta».

## Cuentas y transferencias entre titulares

Registra las cuentas de cada titular (solo los últimos 4 dígitos, en la base
local):

```powershell
.\scripts\agente-local.ps1 owner-account -Owner santiago -Suffix NNNN
.\scripts\agente-local.ps1 owner-account -Owner manuela -Suffix NNNN
```

- Registrar una cuenta revisa todos los movimientos existentes: los que nombran
  una cuenta registrada del otro titular (`*NNNN` o un destino `tr:` que termina en
  esos dígitos) se marcan **entre Santiago y Manuela**. La marca automática nunca
  quita una puesta a mano.
- También se marca a mano con el botón «Entre Santiago y Manuela» de la tarjeta del
  destino, que vale para pagos futuros a ese destino.
- Las cuentas propias también se le indican a Ollama, para que una transferencia a
  ellas quede como transferencia propia.

**Completar destinos ya importados.** Los avisos nuevos guardan el destino del
pago al importarse. Para los importados antes:

```powershell
.\scripts\agente-local.ps1 backfill-destinations
```

Vuelve a leer por Graph, en solo lectura y sin Ollama, los avisos de pagos
salientes de Santiago sin destino. Solo completa destinos vacíos (auditados como
`destination-backfill`); no cambia importes, categorías ni decisiones.

## Conciliar extractos

Requiere `statements.json` ([configuración](configuracion.md#6-extractos-y-créditos-opcional)).
Se ejecuta solo con cada `scan` o `import`, o a mano:

```powershell
.\scripts\agente-local.ps1 reconcile-statements -Start 2026-01-01 -ReadOnly  # solo informa
.\scripts\agente-local.ps1 reconcile-statements                               # importa faltantes
```

**Extractos del banco** (cuenta de ahorros, tarjetas en pesos y dólares,
Fiducuenta, crédito de consumo e hipotecario):

- Se buscan en **todos los buzones** configurados (el Outlook de Santiago por
  Graph y el Gmail de Manuela por la API de Gmail), solo en los correos de los
  remitentes de `statements.json` y solo los adjuntos con nombre de extracto. En
  Gmail se piden solo los nombres y tamaños de las partes del correo, nunca su
  texto. Un correo de otro remitente detiene la ejecución. Cada PDF se descifra
  con la contraseña de su titular y se lee en memoria; no se guarda.
- **Cada extracto es del titular del buzón donde llegó**, y sus movimientos solo
  se comparan con los de ese titular: lo que falta en un extracto de Manuela
  entra pendiente a nombre de Manuela. Si el mismo extracto de un producto
  (mismo número y corte) llega a los buzones de los dos, no se sabe de quién es:
  la conciliación se detiene sin importar nada.
- Se leen con lectores deterministas, sin Ollama. Un extracto que no se lee con
  exactitud detiene todo sin importar nada.
- El de ahorros se lee conservando cada fila impresa (desde la segunda página el
  texto simple separa fechas, descripciones y valores) y se valida dos veces: el
  saldo corrido, línea a línea, y el resumen del extracto. Saldo anterior más
  abonos menos cargos debe dar el saldo actual, y las líneas leídas deben sumar
  exactamente los abonos y los cargos del resumen. Si falta una sola línea, se
  detiene.
- El mismo dinero visto en dos extractos se cuenta una vez (pago de una tarjeta,
  adición o retiro del fondo, cuota de un crédito), y se omiten los movimientos
  que extractos consecutivos repiten.
- Un movimiento del extracto **coincide** con uno conocido del mismo importe y
  moneda con hasta 5 días de diferencia; cada uno se usa una sola vez. Lo que solo
  muestra el extracto entra **pendiente** (`statement-import`) y lo decide una
  persona; si es una compra, con una categoría sugerida (ver
  [Sugerencias de categoría](#sugerencias-de-categoría-para-pendientes)). Repetir no
  importa nada dos veces.
- Excepción: las líneas con un texto fijo que los dos titulares confirmaron que
  siempre significa lo mismo quedan decididas al registrar extractos
  (`decision-statement-phrase`), también las que ya estaban pendientes:

  | Texto exacto | Dónde | Decisión |
  |---|---|---|
  | «ABONO SUCURSAL VIRTUAL» | Extracto de tarjeta de crédito | **No cuenta**: pago propio desde la cuenta de ahorros; el gasto son las compras |
  | «PAGO SUC VIRT TC MASTER», «… AMEX» o «… VISA», con o sin «DOLAR» | Cuenta de ahorros | **No cuenta**: la salida con la que pagaste tu tarjeta |
  | «ABONO INTERESES AHORROS» | Cuenta de ahorros | **Ingreso**, categoría «Otros ingresos» |

  Cualquier otro texto, aunque se parezca (por ejemplo «PAGO AUTOM TC…»), sigue
  pendiente. Nunca cambia una decisión tomada y se puede corregir en el historial.
- Se guarda el saldo de cada inversión de Fiducuenta y el saldo de capital de cada
  crédito.
- Las cuotas de crédito cuentan como gasto si se deciden Personal o Familiar.
- Las recargas de tarjetas prepagadas (por ejemplo, «RECARGA DE TARJETA CIVICA»,
  el pase del metro de Medellín) entran como compra, no como pago de tarjeta.
- Si un lector mejora, al conciliar de nuevo se corrige el tipo de los movimientos
  importados de extractos que siguen pendientes (`statement-reclassify`).

**Extractos de Protección** (agrega `afp_proteccion@proteccion.com` a
`statements.json`; se abren con la misma contraseña del titular). No traen número
de producto, así que el buzón dice de quién es cada uno:

| Producto | Correo | Adjunto | Frecuencia |
|---|---|---|---|
| Pensión voluntaria | «Extracto Pensiones Voluntarias Protección S.A.» | `<id>_PensionVoluntaria_AAAAMM.pdf` | Mensual |
| Cesantías | «Extracto de Cesantias Protección.» | `<id>_PensionesCesantias_AAAAMM.pdf` | Semestral |

- Solo guardan cifras, no movimientos: no duplican los avisos de aportes ni
  cambian el gasto. Por producto y período: saldo inicial, aportes, retiros
  (incluidos traslados), rendimientos y saldo final. Los rendimientos son la
  diferencia del saldo descontados aportes y retiros, así que incluyen la comisión.
- Las cifras deben sumar el saldo final con una diferencia de hasta $10, porque
  Protección redondea a pesos. Una diferencia mayor o un concepto que falte detiene
  todo sin guardar nada.
- Los correos de Protección no se envían a Ollama: se marcan como procesados sin
  movimientos, porque la información la aporta el PDF.

En el panel, «Extractos y saldos» muestra el último saldo conocido y cuántos
movimientos de cada extracto coinciden en el mes. Cada titular ve sus extractos;
**Hogar** los ve todos, con el nombre del titular. El grupo **Inversiones** suma
Fiducuenta, pensión voluntaria y cesantías.

## Conciliación automática del mes

Después de cada lectura (`scan`, `import`, la tarea programada), de
`reconcile-statements`, `import-manuela` e `import-credit-ledger`, y de cada
decisión en el panel, el agente revisa los meses ya cerrados (anteriores al mes en
curso). Un mes queda **Conciliado** si se cumple todo:

1. Cada producto **de cualquier titular** que lista movimientos (cuenta de
   ahorros, tarjetas y Fiducuenta) y que ya existía ese mes tiene extractos que
   cubren todos sus días
   (en las tarjetas, los de ese mes y el siguiente; ver abajo).
   Los créditos (solo traen saldos; sus pagos salen de la cuenta de ahorros) y
   Protección (solo cifras) no cuentan.
2. Ninguno de esos extractos dejó movimientos sin coincidir.
3. No hay movimientos pendientes de decisión ese mes, **de ningún titular**.
4. No hay avisos de ese mes en la cola «Avisos sin extraer», de ningún buzón.

- La nota dice con cuántos extractos y de qué titulares se concilió. Queda
  auditado como `coverage-reconciled`.
- Si una condición deja de cumplirse (por ejemplo, una nueva lectura de un extracto
  encuentra un movimiento sin coincidir), el mes vuelve a parcial con el motivo
  (`coverage-reconcile-revoked`).
- Un mes cerrado que aún no se concilia muestra en su nota el motivo, por ejemplo
  «falta el extracto de CTA_AHORROS *NNNN de Manuela que cubre el 2026-10-01».
- Nunca reemplaza tu revisión manual: un mes **Revisado** sigue así.
- **Tarjetas.** Cada extracto lleva el mes de su fecha de corte (a mitad de mes) y
  lista los movimientos desde el corte anterior: el extracto de noviembre trae las
  compras de la segunda mitad de octubre. Por eso un mes se concilia cuando llegan
  los extractos de la tarjeta de ese mes **y del siguiente**. Esos días son también
  los que más suelen traer compras sin aviso del banco, que entran pendientes desde
  el extracto aunque el mes ya haya pasado.
- La cuenta de ahorros envía extractos **trimestrales**, así que un mes se concilia
  cuando llega el extracto de su trimestre (los de octubre a diciembre, en enero).
- **Tarjeta con número nuevo.** Si llegan extractos de una tarjeta de crédito con un
  número que no se había visto y el mismo titular ya tenía otra tarjeta con
  extractos anteriores, el panel pregunta en **Decisiones › Tarjetas con número
  nuevo** qué tarjeta es:
  - «Es la Visa \*NNNN con número nuevo»: elige la tarjeta anterior del mismo
    titular que reemplaza (primero las de la misma franquicia y las que dejaron de
    enviar extractos más recientemente). Los extractos de los dos números se juntan
    en una sola tarjeta, y los meses del cambio se concilian con el extracto final
    del número viejo y el primero del nuevo.
  - «Es una tarjeta de crédito nueva»: se concilia por su cuenta desde su primer
    extracto, y la tarjeta anterior se sigue exigiendo.

  Mientras no respondas, los meses desde su primer extracto no se concilian y su
  nota lo dice. La respuesta pide confirmación, queda auditada
  (`card-series-local-user`) y no se cambia desde el panel. Las tarjetas que
  aparecen desde los primeros extractos no se preguntan.
- Si un producto deja de enviar extractos sin reemplazo (por ejemplo, una tarjeta
  cancelada), los meses siguientes ya no se concilian solos; márcalos como revisados
  a mano.

## Créditos y nómina

**Cuotas descontadas de la nómina.** Si la cuota de un crédito se descuenta de la
nómina, el aviso de nómina llega neto y la cuota también cuenta como gasto: el
descuento restaría dos veces. Registra el crédito una vez:

```powershell
.\scripts\agente-local.ps1 payroll-deduction -Owner santiago -Suffix NNNN
```

Desde entonces, cada cuota de ese crédito decidida Personal o Familiar crea un
ingreso «Nómina» espejo por el mismo valor y fecha (`payroll-deduction-income`).
Si la cuota deja de contar, el espejo desaparece. Aplica a las cuotas ya decididas
y a las futuras.

**Pagos desde tu registro en Excel.** El extracto de un crédito solo muestra el
último pago de cada periodo. Si llevas tus pagos en Excel (configurado en
`credit_ledgers.json`):

```powershell
.\scripts\agente-local.ps1 import-credit-ledger
```

- **Hoja.** Encabezados en cualquiera de las primeras 10 filas: «Fecha», «Pago
  Capital», «Pago Intereses», «Seguros y Cargos Adicionales» y «Pago Total»;
  «Abono Capital» es opcional (sin ella, una fila con solo capital es un abono).
  Las demás columnas se ignoran.
- **Contraste obligatorio.** El registro debe coincidir con el banco en cada
  periodo, dentro de 1 peso (por capital o por pagos, según `contrast`), y cada
  fila debe sumar su total (con hasta 2 centavos de diferencia por fórmulas). Si
  algo no cuadra, no se importa nada. Solo se leen las filas desde el primer
  extracto disponible; las filas con total 0 se omiten.
- **Reemplaza al extracto.** Las cuotas de ese crédito leídas del extracto se
  eliminan (con auditoría) y la conciliación deja de importarlas.
- Cada pago entra **pendiente**, con capital, intereses y seguros en la nota, en el
  destino `cr:NNNN`.
- **Origen del dinero.** Las cuotas quincenales son descuento de nómina; un abono
  a capital en un mes de `bonus_months` es descuento de prima y queda como ingreso
  pendiente; fuera de esos meses salió de tu cuenta y no suma ingreso.
- Un movimiento de tu cuenta con el mismo valor a menos de 3 días de un pago del
  registro queda anotado para que lo marques «No cuenta».
- Los créditos registrados se le indican a Ollama, para que un aviso de pago a
  ellos se clasifique como préstamo.

## Unir categorías

```powershell
.\scripts\agente-local.ps1 merge-category -FromCategory Predial -ToCategory Vivienda
```

Mueve los movimientos y las reglas de la primera categoría a la segunda (con
auditoría) y la quita de la lista. Decisiones e importes no cambian.

## Cierre y tarea programada

La [tarea programada](configuracion.md#9-tarea-programada-del-cierre-opcional)
corre el **último día de cada mes a las 22:00**. Si el equipo estaba apagado o sin
tu sesión de Windows, corre en cuanto lo enciendas e inicies sesión.

- **Qué mes cierra.** El último mes cuyo cierre ya venció y que no se ha cerrado.
  Cada cierre queda registrado y auditado (`scheduled-close`), así que nunca cierra
  dos veces el mismo mes. Si se perdieron varios meses, cierra solo el más
  reciente; la lectura incremental igual importa todo lo pendiente.
- **Qué hace.** Comprueba las sesiones, lee todos los buzones (solo lo nuevo),
  concilia extractos y créditos y escribe el resumen en `last-close.txt`.
- **Sesiones vencidas.** Si la de Microsoft venció, se detiene y pide `login`. Si
  venció la del Gmail de Manuela, abre su inicio de sesión de Google y espera
  15 minutos (necesita `address` en su buzón); si nadie inicia sesión, se detiene
  sin importar nada. Con la app de Google en Testing esto pasa casi cada mes.
- **Si algo falla,** no registra el cierre, muestra una notificación de Windows con
  la ruta del registro y la siguiente ejecución lo reintenta.
- **Requisitos.** Tu sesión de Windows abierta y Ollama instalado. Tiene un límite
  de 2 horas y no se detiene por estar con batería.

Un cierre a las 22:00 no incluye compras posteriores: vuelve a importar ese mes al
día siguiente antes de conciliarlo. Para ver el resumen de un mes en la terminal
(gasto confirmado, familiar, ingresos, balance y dudas, nunca totales sin
aprobar):

```powershell
.\scripts\agente-local.ps1 summary -Month 2026-10 -Owner hogar
```

## Probar con un solo aviso

```powershell
.\scripts\agente-local.ps1 pilot -ReadOnly   # lectura e identidad, sin Ollama
.\scripts\agente-local.ps1 pilot             # además propone movimientos
```

Toma el aviso autorizado más reciente entre los últimos mensajes de cada carpeta
del Outlook de Santiago, sin recorrer el histórico. Repetirlo no duplica nada.

Si PowerShell no permite scripts, la entrada de Python equivalente es
(requiere Ollama activo para los comandos que lo usan):

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m monthly_close.main serve
.\.venv\Scripts\python.exe -m monthly_close.main scan --read-only --start 2026-10-01 --end 2026-11-01
```

## El panel

Se abre con `.\scripts\agente-local.ps1` en `http://127.0.0.1:8766`, solo en tu
equipo. Arriba eliges el titular (**Santiago / Manuela / Hogar**, Hogar por
defecto) y la moneda; debajo, las pestañas **Mes**, **Año** y **Decisión**. La
vista se guarda en la URL, así que se puede volver a ella. El tema sigue el modo
claro u oscuro del sistema y el botón «Tema» puede fijar uno.

**Mes**

- Meses en orden cronológico con nombre corto y la línea de cobertura del mes.
- Cuatro cifras: **Gasto del mes** (con barra por titular y familiar),
  **Ingresos**, **Balance** y **Por decidir** (lleva a Decisión; solo aparece si
  queda algo pendiente, y sin ella las otras tres van en una fila). Debajo, en una
  fila compacta y solo si existen: inversiones, pagos a tarjetas, abonos de
  terceros, movimientos entre titulares y retiro en efectivo.
- Calendario por día (tocar un día muestra sus movimientos), dona por categoría
  (tocar una categoría abre su detalle), acumulado, días de la semana, medios de
  pago, ingresos, pagos por tarjeta y comercios frecuentes. «Comercios frecuentes»
  agrupa variantes del mismo comercio (por ejemplo `UBER RIDES` y `UBER *TRIP`
  como «Uber»); lo guardado conserva el texto del aviso.
- **Detalle por categoría** separa gastos, inversiones, ingresos y pagos de
  tarjeta, e indica cuántos movimientos faltan por decidir.
- Cada cifra muestra su valor al pasar el mouse, tocar o llegar con el teclado.

**Año**

- Las mismas cuatro cifras para el año (Por decidir se oculta si todo está
  decidido) y chips con el promedio de gasto por mes cerrado (sin el mes en
  curso), el mes de mayor gasto, la inversión neta, retiros, efectivo, abonos y la
  cobertura («X de N» meses revisados a mano o conciliados con los extractos).
- Gráfica de ingresos y gastos, categorías del año con barras y porcentaje, y
  columnas por categoría con los mismos colores que el mes. El mes en curso se ve
  más claro.
- **Resumen por mes:** Gasto, Personal, Familiar, Abonos, Ingresos, Balance (con
  signo y color), Inversiones, Efectivo, Pendiente y Cobertura, con fila de total.
  Un guion (—) significa que no hubo movimientos de ese tipo, nunca gasto cero; los
  meses que aún no llegan dicen «Sin notificaciones del banco aún». La fila del mes
  en curso lleva una barra de color a la izquierda, y el total de Cobertura se
  muestra como «X/Y» (el detalle completo aparece al pasar el cursor).
- Tocar un mes de la tabla o una barra abre ese mes.

**Decisión:** ver [Decidir movimientos](#decidir-movimientos),
[Reglas por comercio](#reglas-por-comercio) y
[Avisos sin extraer](#avisos-sin-extraer).

**Comportamiento seguro.** Durante una carga o un guardado se bloquean los
controles para evitar envíos dobles; si algo falla, se muestra el error y se
puede reintentar con **Actualizar**. Borrar una regla o descartar un aviso pide
confirmación. Nada se decide sin pulsar un botón de aprobación. El panel
responde solo a sí mismo (Host, Origin y token por ejecución).

## Lectura de los buzones

### Outlook (Microsoft Graph)

- **Permisos.** Permiso `Mail.Read` y cliente limitado a `GET`: no puede enviar,
  mover, borrar ni marcar mensajes. El token solo se envía a
  `graph.microsoft.com/v1.0`; cualquier otro destino, incluido un enlace de
  paginación, detiene la ejecución. `scan` y la tarea nunca abren un navegador.
- **Carpetas.** Bandeja de entrada, Correo no deseado, Elementos eliminados y
  **Dinero** con todas sus subcarpetas. Si falta Dinero o cambia su árbol durante
  el recorrido, se detiene sin declarar cobertura completa.
- **Recorrido completo.** Lee cada carpeta de la más reciente a la más antigua y la
  contrasta con el total que anuncia Graph antes y después; si no coincide tras
  tres intentos, se detiene. El primer aviso anterior a `-Start` termina esa
  carpeta. Un orden inesperado, identificadores repetidos o superar `max_messages`
  también detienen el recorrido.
- **Cada mensaje.** Se descarga su origen (máximo 2 MB, en memoria) y se exige un
  único `From`, `Subject`, `Message-ID` y `Date` que coincidan con el listado, con
  fechas con zona horaria. Solo se decodifica el texto (o el HTML sin scripts ni
  recursos remotos). No se guardan adjuntos.
- **Identidad.** Cada mensaje se identifica con un hash de su origen
  (`outlook.live.com`) y su `Message-ID`; ese origen es fijo.
- **Reintentos.** Los fallos momentáneos (red, 429, 500, 502, 503 y 504) se
  reintentan hasta tres veces.

### Gmail de Manuela

- Permiso `gmail.readonly`. El token solo se envía a
  `gmail.googleapis.com/gmail/v1/users/me/`; la renovación usa el servidor de
  Google.
- Se buscan los mensajes de los remitentes aprobados en todo el buzón, incluidos
  Spam y Papelera, sin enviados ni borradores. El remitente debe coincidir
  exactamente. Cada mensaje pasa por las mismas validaciones y la misma extracción
  que Outlook. No se leen adjuntos.
- Antes de leer cualquier buzón se comprueba la sesión de Gmail, para que una
  sesión vencida detenga todo antes de importar.

### Lectura incremental

Sin `-Start` ni `-End`, `scan`, `import` y la tarea leen solo lo que cambió:

- **Outlook** guarda por carpeta el `deltaLink` de la
  [consulta delta de Graph](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0).
  La primera sincronización lista la carpeta completa y descarta, sin abrirlos, los
  avisos anteriores al inicio. Los mensajes borrados o movidos se ignoran.
- **Gmail** guarda el `historyId` del buzón y luego pide solo lo añadido.
- **Inicio.** La primera vez, desde el 1 de enero del año local (o desde el último
  día importado por archivo para ese titular). No hay fecha de fin.
- **Fallos.** La posición de cada carpeta se guarda solo después de procesar todos
  sus avisos, así que un fallo repite esos cambios sin duplicar nada. Si Graph o
  Gmail rechazan una posición caducada, esa carpeta o ese buzón se sincroniza de
  nuevo desde cero.
- **Cola de revisión.** Como Graph y Gmail no vuelven a entregar un aviso, al final
  de cada ejecución se busca de nuevo cada aviso de la cola (mismo remitente y
  ±15 minutos, solo metadatos).
- Este modo no repite la comprobación de totales por carpeta: para demostrar la
  cobertura de un rango, usa el recorrido completo con fechas.

## Frases del banco leídas sin Ollama

Estas frases fijas se leen con reglas deterministas; el modelo no interviene.

| Frase del aviso | Resultado |
|---|---|
| «Pagaste $X en la tarjeta de credito *NNNN desde la cuenta *NNNN, el DD/MM/AAAA» | Pago de tarjeta decidido «No cuenta» (no es gasto: el gasto son las compras). La cuenta queda `Desconocida` |
| «Transferiste $X [por QR] desde tu cuenta *NNNN a la cuenta *NNNN, el …» | Transferencia pendiente a `cuenta *NNNN`, destino `tr:NNNN` |
| «Transferiste $X a la llave @alias o número desde tu cuenta …» (Bre-B) | Destino `bb:@alias` o `bb:NNNN` |
| «Pagaste $X por código QR desde tu cuenta … a la llave …» | Destino `qr:…` |
| «Retiraste $X de tu cuenta de inversión *XXXX …» | Destino `fd:XXXX` |
| «NOMBRE hizo un abono por $X a tu tarjeta de crédito terminada en *N» | Pago a tarjeta pendiente: «Abono de tercero» si pagó otra persona, «No cuenta» si fuiste tú |
| «transferiste $X a la llave K desde tu cuenta *N a NOMBRE» | Transferencia a NOMBRE con la llave y la hora |
| «recibiste una transferencia de NOMBRE por $X» | Ingreso pendiente con el nombre de quien envía |

Las tres últimas frases se reconocen aunque el correo las parta en varias líneas
(por ejemplo «…hizo» al final de una línea y «un abono por…» en la siguiente).

De las cuentas y llaves numéricas se guardan solo los últimos 4 dígitos. Si la
frase no dice el destino o es ambigua, no se asigna; no se adivinan formatos. Al
modelo se le indica además que «transferiste» o «pagaste» nunca es ingreso y que
el comercio es el destinatario nombrado, no el banco.

## Validación de lo que propone Ollama

- **Comercio literal.** El comercio guardado es siempre texto del aviso. Si el
  modelo solo cambia mayúsculas, espacios o signos, se recupera la forma del
  aviso. En avisos sin comercio (por ejemplo, un retiro del fondo), se conserva el
  fragmento más largo de la descripción del modelo que sí aparece en el aviso
  (solo para tipos desconocido, inversión e ingreso; una compra exige el nombre
  completo).
- **Monto literal.** Un monto que solo difiere en espacios se recupera del aviso;
  otro número se rechaza. Un aviso sin ningún importe marcado (`$`, `COP`, `USD`)
  y sin monto válido se trata como informativo: no deja movimiento. Una plantilla
  sin rellenar («Adicionaste {{valorAdicion}}…») va a la cola.
- **Ingresos.** Un ingreso que el aviso no respalda con palabras de dinero
  recibido se convierte en desconocido.
- **Duplicados.** Dos movimientos indistinguibles en un mismo correo detienen el
  escaneo sin importar ese aviso.
- El modelo no tiene terminal, disco, red ni herramientas; sus respuestas se
  validan y nunca se ejecutan.

## Auditoría

Cada cambio de un movimiento queda en la tabla `audit` de `close.sqlite3` con su
origen. Las etiquetas principales:

| Etiqueta | Origen |
|---|---|
| `decision-local-user`, `decision-group-local-user` | Decisión tuya en el panel, de un movimiento o de un destino |
| `decision-rule`, `decision-group-rule` | Decisión de una regla por comercio o por destino |
| `rule-local-user`, `group-rule-local-user`, `group-rule-between-local-user`, `group-rule-delete-local-user` | Regla por comercio o por destino creada, marcada entre titulares o borrada |
| `decision-manuela`, `group-rule-manuela` | Decisiones importadas del panel anterior de Manuela |
| `statement-import`, `statement-reclassify` | Movimiento importado de un extracto, o su tipo corregido |
| `category-suggestions` | Sugerencias de categoría guardadas o recalculadas (solo el número) |
| `decision-statement-phrase` | Línea de extracto decidida por un texto fijo confirmado (ver «Conciliar extractos») |
| `destination-backfill` | Destino completado con `backfill-destinations` |
| `payroll-deduction-income` | Ingreso espejo de una cuota descontada de la nómina |
| `mailbox-possible-duplicate` | Posible duplicado entre el Gmail de Manuela y su archivo |
| `reprocess-replace-card-payments` | Pagos de tarjeta reemplazados por `-ReprocessEmpty` |
| `category-merge-local-user` | Categoría unida con `merge-category` |
| `scheduled-close` | Cierre de la tarea programada |
| `coverage`, `coverage-reconciled`, `coverage-reconcile-revoked` | Mes revisado a mano; mes conciliado automáticamente; conciliación retirada |
| `migrate-household` | Migración de una base anterior |

**Migración de bases anteriores.** Al abrir una base de una versión anterior, el
agente la copia a `close.sqlite3.backup-<fecha UTC>` antes de añadir columnas y
tablas. No cambia importes, decisiones ni auditoría.

## Privacidad y límites

- `scan` lista remitente, asunto, fecha e identificador de los mensajes de las
  carpetas del alcance para verificar el recorrido. Solo abre el origen de los
  remitentes aprobados; los demás no se abren ni se envían a Ollama.
- No se guardan cuerpos de correo en la base ni en los registros. Se conservan los
  datos financieros, las decisiones y la auditoría hasta que los borres.
- Para borrar todos los datos del cierre: detén el panel, las lecturas y la tarea,
  y borra `close.sqlite3` (se pierden decisiones e histórico) y las sesiones. No
  afecta a facturas.
- El sistema no tiene acceso a una API bancaria: la única forma de confirmar que
  un mes está completo es compararlo con los extractos.

## Estado

**Verificado con datos reales**

- Lectura de Outlook (2026-11-14): 15 carpetas, 8303 filas de metadatos, 1716 de
  remitentes autorizados, totales coincidentes; importaciones de 2026 con Ollama.
- Lectura incremental (2026-11-16): primera sincronización de Outlook y Gmail en
  5 minutos sin duplicados y una segunda sin cambios en 44 s.
- Gmail de Manuela (2026-11-16): inicio de sesión en modo Testing, recorrido de
  solo lectura (3 avisos cotejados en Gmail) y primera importación con un posible
  duplicado y una transferencia propia, ambos pendientes.
- Importación de Manuela: paridad mes a mes con su panel (salvo la diferencia
  aprobada del cajero); la segunda importación no cambió nada; los totales de
  Santiago quedaron idénticos.
- Extractos del banco de 2026: leídos sin errores; una segunda ejecución no
  importó nada. Extractos de Protección (2026-11-16): 10 de pensión voluntaria en
  los dos formatos y 2 de cesantías, encadenados sin diferencias mayores a $10.
- `backfill-destinations`: guardó el destino de casi todos los pagos de Santiago
  sin cambiar importes ni decisiones.
- Recuperación de avisos (2026-11-14/15): 9 ingresos y 8 gastos recuperados con
  `-ReprocessEmpty`, 9 pagos de tarjeta leídos, y la cola de 15 avisos quedó vacía
  (9 informativos, 2 inversiones y 4 movimientos recuperados, más 12
  transferencias a cuenta).
- Conciliación automática (2026-11-18), con `import` en la base real: enero a
  septiembre de 2026 quedaron conciliados. Octubre sigue parcial: faltan el extracto
  de noviembre de la Amex y el de ahorros del cuarto trimestre.
- Extractos del Gmail de Manuela (2026-11-18): el lector de Gmail descargó en
  solo lectura los PDF de extractos desde diciembre de 2025 (ahorros, Fiducuenta,
  Mastercard, Visa y cesantías).
- Extractos por titular (2026-11-19), `reconcile-statements -ReadOnly`: 111
  extractos de Santiago y 31 de Manuela, cada buzón con la contraseña de su
  titular, sin errores. El lector de ahorros solo leía la primera página de cada
  trimestre (33 líneas; se saltaban entre 88 y 151 por extracto). Corregido, los 20
  extractos de ahorros de los dos titulares cuadran con su resumen. Quedan sin
  movimiento conocido 206 líneas de ahorros de Santiago y 365 de Manuela,
  sobre todo abonos diarios de intereses (123 y 153). La siguiente importación
  las deja pendientes y los meses conciliados con esas líneas vuelven a parcial.
- Abono a tarjeta partido en dos líneas (2026-11-19): un aviso real «…hizo⏎un
  abono por…» quedaba en cola porque el lector fijo no lo reconocía y Ollama lo
  proponía como préstamo. Leído de nuevo en solo lectura, ahora sale sin Ollama
  como un pago de tarjeta pendiente.
- Sugerencias de reglas con Ollama: probadas por el usuario en el panel con datos
  reales (2026-11-19); una regla sugerida, ajustada y aprobada.
- Sugerencias de categoría (2026-11-19), en solo lectura sobre la base real con
  `qwen2.5:7b`: 62 de 64 compras pendientes de extractos con sugerencia (9 por
  regla, 51 por decisiones anteriores, 2 por Ollama), frente a 30 solo con
  Ollama. En tres muestras de comercios ya decididos y ocultados del historial
  (148 en total), Ollama coincidió con tu categoría en 45 sin ejemplos (30 %),
  en 59-65 con 3 ejemplos por comercio (40-44 %) y en 71-75 con 5 (48-51 %), en
  varias corridas; acierta en cerca de la mitad de lo que responde, así que sus
  sugerencias hay que revisarlas. Falta decidir con las sugerencias desde
  el panel.
- Las pruebas (`pytest tests\monthly_close`) cubren reglas, importes, zona
  horaria, decisiones, lectores simulados de Graph y Gmail, el lanzador y el panel
  en Chromium (incluido un contraste mínimo de 4,5:1 en ambos temas).

**Pendiente**

- Importar (no solo leer) los extractos de los dos titulares con el lector de
  ahorros corregido, decidir los pendientes que dejen y ver qué meses vuelven a
  conciliarse.
- Responder la primera pregunta de tarjeta con número nuevo: la Visa de Manuela,
  con extractos desde septiembre de 2026, y su Mastercard como única opción (las
  dos tienen extractos de septiembre y octubre). Probado solo sobre una copia de la
  base y con datos sintéticos.

- La primera ejecución real de la tarea programada.
- Cuánto dura en la práctica la sesión de Gmail en Testing, y si pasar la app a
  producción lo evita.
- Avisos reales en USD y las tasas elegidas.
- Un `deltaLink` o un `historyId` caducados con datos reales.
- Las transferencias entre titulares no se detectan solas en los datos actuales
  (los avisos de Santiago no traen la cuenta destino); por ahora se marcan a mano
  por destino.
