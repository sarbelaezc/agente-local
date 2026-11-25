# Informes de Invesbot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Leer en cada recorrido los informes de Invesbot descargados a mano, guardar el saldo y las cifras de cada plan, mostrarlos en Inversiones y confirmar los aportes, devolviendo a Inversión los «No cuenta» que el informe demuestra.

**Architecture:** Un módulo nuevo `monthly_close/invesbot.py` lee y valida los PDF (con `pypdf`, como los extractos) y calcula, sin tocar la base, las cifras del período y qué aportes corregir. `Store` guarda las cifras en `statement_checks` (producto `INVESBOT`) y aplica las correcciones auditadas. `main.run_complements` lo encadena después de los extractos. El panel trata `INVESBOT` como producto de inversión.

**Tech Stack:** Python 3.12, `pypdf`, SQLite, pytest, Playwright (panel).

**Spec:** `docs/superpowers/specs/2026-11-25-invesbot-informes-design.md`

## Global Constraints

- Ningún informe real ni cifra real en el repositorio: pruebas con texto sintético.
- Sin Ollama; el texto se lee en memoria; nada del PDF se guarda salvo las cifras.
- Un informe ilegible, sin un rótulo o cuyas cifras no suman detiene todo sin guardar nada (`ValueError`, que `_run_reporting` ya muestra como «Detenido»).
- Validación: apertura + aportes adicionales − retiros + rendimientos acumulados = saldo, exacto al centavo.
- No se usan «Aportes último mes» ni «Rendimientos último mes».
- Nunca se crean movimientos; solo se cambian aportes del plan «No cuenta» a Inversión.
- Auditoría `invesbot-restore`; un movimiento con esa auditoría no se vuelve a tocar.
- Textos del panel en español con mayúscula inicial (`test_every_text_of_the_panel_starts_in_uppercase`).
- Comandos: `.\.venv\Scripts\python.exe -m pytest tests\monthly_close\<archivo>`; commits Conventional en español con `Co-Authored-By`, push tras cada commit.

## Review Focus

- Un PDF que no es un informe de Invesbot dejado en la carpeta: debe detener con un mensaje que nombre el archivo, no ignorarse en silencio (Task 1).
- El mismo informe descargado dos veces con otro nombre: cuenta una vez; dos con el mismo plan y mes y cifras distintas detienen (Task 1).
- Un informe más antiguo agregado después de uno más nuevo: las cifras del período del siguiente se recalculan (Task 2).
- La persona devuelve a «No cuenta» un aporte corregido: el siguiente `import` no lo toca (Task 3).
- Un titular con dos planes: un «Invesbot» sin nombre de plan no cuenta para ninguno (Task 3).

---

### Task 1: Lector de informes

**Files:**
- Create: `src/monthly_close/invesbot.py`
- Test: `tests/monthly_close/test_monthly_close_invesbot.py`

**Interfaces:**
- Produces:
  - `INVESBOT_DIR = "invesbot"`, `INVESBOT_PRODUCT = "INVESBOT"`.
  - `@dataclass(frozen=True) class Report: owner: str; plan: str; period_end: date; plan_start: date; balance_minor: int; opening_minor: int; additional_minor: int; additional_count: int; withdrawals_minor: int; returns_minor: int` (todos acumulados desde el inicio del plan, salvo `balance_minor` al cierre).
  - `parse_report(text: str, owner: str) -> Report` — lanza `ReportError(ValueError)`.
  - `read_reports(data_dir: Path) -> list[Report]` — lee `data_dir/invesbot/{santiago,manuela}/*.pdf` (otras carpetas no), ordenados por `(owner, plan, period_end)`, sin repetidos.

- [ ] **Step 1: Write the failing tests**

En la prueba, una función `report_text(**cifras)` arma el texto de las páginas 1 y 2 con los rótulos del spec (valores ficticios: apertura 50.000, aportes adicionales 700.000 con «7 aportes», retiros 0, rendimientos 12.345,67, saldo 762.345,67, plan «Plan Ficticio», «Julio de 2026», inicio «Ene 13 de 2026»).

```python
def test_a_report_gives_its_cumulative_figures():
    report = invesbot.parse_report(report_text(), "santiago")
    assert report == invesbot.Report("santiago", "Plan Ficticio", date(2026, 7, 31), date(2026, 1, 13),
                                      76234567, 5000000, 70000000, 7, 0, 1234567)

@pytest.mark.parametrize("change", [
    {"month": "Sptiembre"},                 # how the bank writes it
    {"returns": "-97.181,60", "balance": "652.818,40"},
    {"withdrawals_label": "RETIROSREALIZADOS"},
])
def test_the_bank_wordings_are_read(change): ...  # parse_report no falla y lee el valor

@pytest.mark.parametrize("change,error", [
    ({"balance": "762.345,68"}, "no suman"),
    ({"drop": "MONTO DE APERTURA"}, "MONTO DE APERTURA"),
    ({"month": "Juliembre"}, "periodo"),
])
def test_a_report_that_is_not_exact_stops(change, error):
    with pytest.raises(invesbot.ReportError, match=error): ...

def test_the_folders_are_read_by_owner_once_and_conflicts_stop(tmp_path, monkeypatch):
    # monkeypatch invesbot._pdf_text para devolver report_text(...) por nombre de archivo
    # santiago/a.pdf y santiago/b.pdf con el mismo texto -> un solo Report
    # manuela/c.pdf -> owner "manuela"; otra/d.pdf -> ignorado
    # santiago/e.pdf mismo plan y mes con saldo distinto -> ReportError que nombra "e.pdf"

def test_a_pdf_that_is_not_a_report_stops_naming_the_file(tmp_path, monkeypatch): ...
def test_without_folders_there_is_nothing_to_read(tmp_path):
    assert invesbot.read_reports(tmp_path) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\monthly_close\test_monthly_close_invesbot.py -q`
Expected: FAIL (`ModuleNotFoundError: monthly_close.invesbot`).

- [ ] **Step 3: Implement**

`_pdf_text(content: bytes) -> str` con `pypdf.PdfReader` (páginas 1 y 2, como `statements.decrypt_text`, límite `statements.MAX_PDF_BYTES`; cifrado o ilegible → `ReportError`). Normaliza con `unicodedata.normalize("NFKC")` (el PDF trae ligaduras como «ﬁ»). Meses: `Enero…Diciembre`, más `Sptiembre`; inicio en abreviatura de 3 letras con `Dec`/`Dic`. Montos `$ 1.234,56`, con signo en rendimientos. Período = último día del mes. Errores en español nombrando el rótulo o el archivo, nunca cifras.

- [ ] **Step 4: Run to verify they pass** — mismo comando, Expected: PASS.

- [ ] **Step 5: Commit** `feat(monthly-close): lee los informes de Invesbot`.

### Task 2: Cifras del período y guardado

**Files:**
- Modify: `src/monthly_close/invesbot.py`
- Modify: `src/monthly_close/store.py` (migración de columnas de `statement_checks`, cerca de la línea 236)
- Test: `tests/monthly_close/test_monthly_close_invesbot.py`

**Interfaces:**
- Consumes: `Report` (Task 1).
- Produces:
  - `period_checks(reports: list[Report]) -> list[dict]` — una fila por informe para `Store.record_statements(checks, [])`, con `identity = f"INVESBOT:{plan}:{period_end}:{owner}"`, `product="INVESBOT"`, `suffix=plan`, `owner`, `period_start` (día 1 del mes), `period_end`, `movements=matched=missing=0`, `balance_minor`, `opening_minor`, `contributions_minor`, `withdrawals_minor`, `returns_minor`.
  - Columnas nuevas `expected_contributions_minor`, `recorded_contributions_minor` (INTEGER, nulas) en `statement_checks`; Task 3 las llena.

- [ ] **Step 1: Write the failing tests**

```python
def test_period_figures_come_from_the_previous_report():
    feb = report(month=2, opening=50_000, additional=100_000, balance=150_500)
    may = report(month=5, opening=50_000, additional=400_000, balance=452_000)  # march-april missing
    [first, second] = invesbot.period_checks([may, feb])
    assert (first["opening_minor"], first["contributions_minor"], first["returns_minor"]) == (0, 15000000, 50000)
    assert (second["opening_minor"], second["contributions_minor"], second["withdrawals_minor"],
            second["returns_minor"]) == (15050000, 30000000, 0, 150000)
    assert second["identity"] == "INVESBOT:Plan Ficticio:2026-05-31:santiago"

def test_a_loss_is_a_negative_return(): ...

def test_checks_are_stored_and_repeating_changes_nothing(make_store):
    store = make_store(); checks = invesbot.period_checks([...])
    store.record_statements(checks, []); store.record_statements(checks, [])
    rows = store.state()["statement_checks"]  # dos filas INVESBOT con sus cifras
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL (`period_checks` no existe).
- [ ] **Step 3: Implement** `period_checks`; agregar las dos columnas a la migración de `statement_checks` (mismo patrón que `INVESTMENT_FIGURES`).
- [ ] **Step 4: Run to verify they pass**, y `tests\monthly_close\test_monthly_close_reconciliation.py` sigue en verde.
- [ ] **Step 5: Commit** `feat(monthly-close): guarda las cifras de cada plan de Invesbot`.

### Task 3: Comparación de aportes y corrección

**Files:**
- Modify: `src/monthly_close/invesbot.py`
- Modify: `src/monthly_close/store.py`
- Test: `tests/monthly_close/test_monthly_close_invesbot.py`

**Interfaces:**
- Consumes: `Report`, `period_checks` (Tasks 1-2).
- Produces:
  - `@dataclass(frozen=True) class Outcome: restore: list[str]; differences: dict[str, tuple[int, int]]` — ids a pasar a Inversión, y por `identity` de informe `(esperado, registrado)` tras corregir.
  - `plan_outcome(reports: list[Report], movements: list[dict], plans_by_owner: dict[str, set[str]], restored: set[str]) -> Outcome` — puro. `movements` son filas de `transactions` del titular (`id, day, merchant, amount_minor, kind, decision`).
  - `Store.invesbot_movements(owner: str) -> list[dict]` — movimientos del titular con comercio que empieza por «Invesbot» o igual a «TRASLADO A FONDO DE INVERSION», en COP.
  - `Store.restored_invesbot_ids() -> set[str]` — targets de la auditoría `invesbot-restore`.
  - `Store.apply_invesbot(checks: list[dict], outcome: Outcome) -> int` — una transacción: `record_statements`-equivalente para `checks`, llena las columnas de Task 2, y para cada id de `outcome.restore` decide `investment` vía `_decide(db, id, "investment", audit=("invesbot-restore", identity))` y escribe la nota «Contado como aporte: el informe de Invesbot de <mes> muestra el aporte.». Devuelve los corregidos.

El algoritmo, por plan y de más antiguo a más nuevo (spec «Comparación y corrección»):

```
esperado = apertura + adicionales - retiros (acumulado del informe)
registrado = suma(investment) - suma(investment_withdrawal) de movimientos del plan con plan_start <= day <= period_end
falta = esperado - registrado
si falta > 0: traslados del mes decididos investment, del más antiguo, mientras amount <= falta -> cuentan
si falta > 0: «Invesbot <plan>»/«Invesbot» del mes con decision 'exclude', id no en restored,
              del más antiguo, mientras amount <= falta -> restore (y cuentan)
differences[identity] = (esperado, registrado final)
```

«Del plan por nombre»: `Invesbot <plan>` exacto, o `Invesbot` sin plan solo si `plans_by_owner[owner] == {plan}`. Un traslado contado para un mes no se vuelve a contar en otro.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_spec_example_restores_july_counts_the_august_transfer_and_warns():
    # informes jul (esperado 700) y ago (800); aportes «Invesbot Plan Ficticio» de 100 hasta junio (600),
    # «Invesbot» excluido el 2026-07-01 (id "jul"), traslado de 100 decidido investment el 2026-08-31 ("tr"),
    # «Invesbot» excluido el 2026-08-03 ("ago")
    outcome = invesbot.plan_outcome(reports, movements, {"santiago": {"Plan Ficticio"}}, set())
    assert outcome.restore == ["jul"]
    assert outcome.differences["INVESBOT:Plan Ficticio:2026-08-31:santiago"] == (80000000, 80000000)

def test_a_transfer_that_does_not_fit_is_another_fund(): ...      # traslado de 9.000 con falta de 100: no cuenta, diferencia 100
def test_a_restored_movement_the_person_reverted_is_left_alone(): # id en restored -> no vuelve a restore
def test_bare_invesbot_counts_only_with_one_plan(): ...           # dos planes -> «Invesbot» no cuenta
def test_only_excluded_plan_contributions_are_restored(): ...     # pendiente o de otro titular/comercio: nunca

def test_applying_is_audited_and_repeating_changes_nothing(make_store):
    # store con los movimientos; apply_invesbot dos veces -> 1 corregido y luego 0;
    # decision 'investment', auditoría invesbot-restore con la identity, nota en español;
    # número de filas de transactions igual antes y después
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement** `plan_outcome` y los tres métodos de `Store`.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** `feat(monthly-close): confirma los aportes de Invesbot con sus informes`.

### Task 4: En el recorrido

**Files:**
- Modify: `src/monthly_close/main.py` (`run_complements`, cerca de la línea 785)
- Test: `tests/monthly_close/test_monthly_close_invesbot.py`

**Interfaces:**
- Consumes: `read_reports`, `period_checks`, `plan_outcome`, `Store.invesbot_movements`, `Store.restored_invesbot_ids`, `Store.apply_invesbot`.
- Produces: `import_invesbot_reports(store: Store, data_dir: Path) -> list[str]` en `main.py` — líneas para la terminal, sin cifras: «Invesbot <plan> (<titular>): N informes leídos; M aportes contados de nuevo.» y, si la diferencia del informe más reciente no es cero, «Invesbot <plan>: el informe de <mes> cuenta más (o menos) aportes que los registrados; revisa el panel.».

- [ ] **Step 1: Write the failing tests**

```python
def test_complements_read_the_reports_after_the_statements(tmp_path, monkeypatch, make_store): ...
    # sin statements.json ni credit_ledgers.json; read_reports devuelve informes sintéticos ->
    # run_complements guarda las filas INVESBOT y llama a refresh_reconciled_coverage después
def test_an_unreadable_report_stops_the_run_without_storing(tmp_path, monkeypatch, make_store): ...
def test_without_reports_nothing_is_printed(tmp_path, make_store, capsys): ...
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `import_invesbot_reports` y llamarla en `run_complements` después de los registros de créditos y antes de `refresh_reconciled_coverage`, solo si `read_reports` devuelve algo.
- [ ] **Step 4: Run** `tests\monthly_close\test_monthly_close_invesbot.py` y `tests\monthly_close\test_monthly_close_scan.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(monthly-close): lee los informes de Invesbot en cada recorrido`.

### Task 5: Panel

**Files:**
- Modify: `src/monthly_close/dashboard.html` (`productNames` y `investmentProducts` ~1574-1580; nombres en ~1603, ~2217, ~2624; `statementRow`)
- Test: `tests/monthly_close/test_monthly_close_dashboard.py`

**Interfaces:**
- Consumes: filas `statement_checks` con `product == "INVESBOT"`, `suffix` = plan, columnas de Task 2.

- [ ] **Step 1: Write the failing test** `test_invesbot_plans_are_investments_with_their_figures` (Chromium, con un store sintético): en Mes, bloque Inversiones, la fila dice «Invesbot Plan Ficticio» (sin «*»), su saldo y «aportes … · retiros … · rendimientos …»; con `expected ≠ recorded` en el informe más reciente, una línea «El informe cuenta 800.000 en aportes; hay 700.000 registrados»; en Año, el plan está en la lista de productos de Inversiones. `test_every_text_of_the_panel_starts_in_uppercase` sigue en verde.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement**: `INVESBOT: "Invesbot"` en `productNames`; `"INVESBOT"` en `investmentProducts`; una función `productLabel(product, suffix)` que use espacio en vez de « *» para `INVESBOT` y reemplace las tres construcciones del nombre; la línea de diferencia en `statementRow` solo para el informe más reciente de cada plan.
- [ ] **Step 4: Run** `tests\monthly_close\test_monthly_close_dashboard.py tests\monthly_close\test_monthly_close_household_dashboard.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(monthly-close): muestra los planes de Invesbot en Inversiones`.

### Task 6: Documentación y validación con datos reales

**Files:**
- Modify: `README.md` (sección del cierre), `docs/cierre-mensual.md` (Conciliar extractos, Inversiones del panel, Estado), `docs/configuracion.md` (sección 6, carpetas `invesbot`)

- [ ] **Step 1: Documentar** qué carpeta, qué se lee, la corrección automática y su auditoría, el aviso, y que un informe inválido detiene. Sin cifras ni nombres de planes reales.
- [ ] **Step 2: Suite completa** `.\.venv\Scripts\python.exe -m pytest`. Expected: todo PASS.
- [ ] **Step 3: Validación en solo lectura**: copiar `close.sqlite3` a la carpeta temporal de la sesión y ejecutar `read_reports` + `period_checks` + `plan_outcome` sobre la copia con los informes reales; reportar al usuario solo conteos (informes leídos, aportes que se corregirían, si hay diferencia). No tocar la base real.
- [ ] **Step 4: Commit** `docs(monthly-close): documenta los informes de Invesbot`; actualizar el Estado con lo verificado tras el `import` real del usuario.
