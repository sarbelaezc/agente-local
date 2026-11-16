"""Bank statements: parsing, reconciliation and import, with fictitious text only."""
from __future__ import annotations

import base64
import io
import json
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest
from pypdf import PdfWriter

from monthly_close import main as app
from monthly_close import statements as st
from monthly_close.domain import Preferences
from monthly_close.graph_reader import GraphSourceReader
from monthly_close.outlook import OutlookError
from monthly_close.store import Store

SAVINGS = """Extracto de cuenta
DESDE: 2026/01/01 HASTA: 2026/03/31
CUENTA DE AHORROS
NÚMERO 12345670001
FECHA DESCRIPCIÓN SUCURSAL DCTO. VALOR SALDO
2/01 PAGO PSE EMPRESA FICTICIA -100,000.00 900,000.00
5/01 PAGO SUC VIRT TC MASTER DOLAR -50,000.00 850,000.00
6/01 TRASLADO A FONDO DE INVERSION -200,000.00 650,000.00
9/02 ABONO INTERESES AHORROS .25 650,000.25
10/03 TRANSFERENCIA DESDE PERSONA FICTICIA 300,000.00 950,000.25
"""
CARD = """TARJETA: ************0002
INFORMACIÓN DE LA TARJETA *0002
Moneda de cuenta en: PESOS
1234 TIENDA FICTICIA 03/01/2026 $ 45.000,00 1/1 $ 45.000,00 0,0000 % 0,0000 % $ 0,00
1234 TIENDA FICTICIA 03/01/2026 $ 45.000,00 1/1 $ 45.000,00 0,0000 % 0,0000 % $ 0,00
TRASLADO SALDO A DIFERIR 04/01/2026 $ -1.000,00 $ -1.000,00 $ 0,00
ABONO SUCURSAL VIRTUALC24280 06/01/2026 $ -50.000,00 $ -50.000,00 $ 0,00
CUOTA DE MANEJO000000 02/01/2026 $ 35.672,00 1/1 $ 35.672,00 0,0000 % 0,0000 % $ 0,00
Moneda de cuenta en: DÓLARES
5678 SERVICIO FICTICIO 08/01/2026 $ 12,34 1/1 $ 12,34 0,0000 % 0,0000 % $ 0,00
movimientos
"""
FUND = """FONDO DE INVERSIÓN COLECTIVA
CUENTA DE INVERSIÓN: 9876543210003
DESDE: 20260101 HASTA: 20260131
SALDO ANTERIOR ADICIONES RETIROS
VALOR EN PESOS VALOR EN UNIDADES VALOR EN PESOS VALOR EN PESOS
1.000.000,00 10,00000000 200.000,00 0,00
RENDIMIENTOS RETENCIÓN NUEVO SALDO
VALOR EN PESOS VALOR EN PESOS VALOR EN PESOS VALOR EN UNIDADES
5.000,00 50,00 1.204.950,00 12,00000000
20260106 ADICION 200.000,00 2,00000000 1.200.000,00
20260131 RENDIMIENTOS 5.000,00 0,05000000 1.205.000,00
"""
CREDIT = """OBLIGACIÓN Nº: 5550004444
FECHA DE PAGO: 15/02/2026
CONCEPTO ABONO ANTERIOR CUOTA A PAGAR
ABONO A CAPITAL 400,000.00 401,000.00
TOTAL 450,000.00 451,000.00
FECHA CORTE EXTRACTO 2/05/2026
SALDO DE CAPITAL 9,000,000.00
"""
MORTGAGE = """EXTRACTO DE CRÉDITO HIPOTECARIO
2026/02/15 2026/01/31 $ 1,500,000.00 $ 150,000,000.00
NÚMERO DE CRÉDITO
11122233344
2026/01/15 PAGO CUOTA 1,000,000.00 480,000.00 0.00 20,000.00 0.00 0.00 0.00 0.00 1,500,000.00
"""


@pytest.fixture
def preferences(categories) -> Preferences:
    return Preferences.parse({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": ["avisos@example.test"], "categories": categories,
        "confidential_confirmed": True,
    })


@pytest.fixture
def store(tmp_path, preferences) -> Store:
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    return store


def test_savings_statement_is_read_and_checked_by_running_balance():
    statement = st.parse("CTA_AHORROS", "202603", SAVINGS)
    assert (statement.suffix, statement.period_end) == ("0001", date(2026, 3, 31))
    assert [(m.day.isoformat(), m.amount_minor, m.hint) for m in statement.movements] == [
        ("2026-01-02", 10000000, "unknown"), ("2026-01-05", 5000000, "card_payment"),
        ("2026-01-06", 20000000, "investment"), ("2026-02-09", -25, "income"),
        ("2026-03-10", -30000000, "income"),
    ]
    broken = SAVINGS.replace("850,000.00", "851,000.00")
    with pytest.raises(st.StatementError, match="saldo corrido"):
        st.parse("CTA_AHORROS", "202603", broken)


def test_card_statement_keeps_currency_sections_and_skips_internal_lines():
    statement = st.parse("TARJETA_MASTERCARD", "202601", CARD)
    assert statement.suffix == "0002"
    assert [(m.description, m.amount_minor, m.currency, m.hint) for m in statement.movements] == [
        ("TIENDA FICTICIA", 4500000, "COP", "purchase"),
        ("ABONO SUCURSAL VIRTUAL", -5000000, "COP", "card_payment"),
        ("CUOTA DE MANEJO", 3567200, "COP", "purchase"),
        ("SERVICIO FICTICIO", 1234, "USD", "purchase"),
    ]


def test_fund_credit_and_mortgage_statements_keep_balances():
    fund = st.parse("FIDUCUENTA", "202601", FUND)
    assert fund.balance_minor == 120495000
    assert [(m.amount_minor, m.hint) for m in fund.movements] == [(20000000, "investment")]
    credit = st.parse("CREDITO", "202602", CREDIT)
    assert (credit.suffix, credit.balance_minor, credit.period_end) == ("4444", 900000000, date(2026, 2, 5))
    assert [(m.amount_minor, m.hint) for m in credit.movements] == [(45000000, "loan")]
    mortgage = st.parse("HIPOTECARIO", "202601", MORTGAGE)
    assert (mortgage.suffix, mortgage.balance_minor) == ("3344", 15000000000)
    assert [(m.day.isoformat(), m.amount_minor) for m in mortgage.movements] == [("2026-01-15", 150000000)]
    with pytest.raises(st.StatementError):
        st.parse("CREDITO", "202602", "texto sin formato")


def test_same_money_in_two_statements_is_counted_once():
    statements = [
        st.parse("CTA_AHORROS", "202603", SAVINGS),
        st.parse("TARJETA_MASTERCARD", "202601", CARD),
        st.parse("FIDUCUENTA", "202601", FUND),
    ]
    candidates = st.pair_internal(statements)
    savings = [c.movement.description for c in candidates if c.statement.product == "CTA_AHORROS"]
    # The card payment and the fund addition are kept from the card and fund statements.
    assert "PAGO SUC VIRT TC MASTER DOLAR" not in savings and "TRASLADO A FONDO DE INVERSION" not in savings
    assert len(candidates) == 5 + 4 + 1 - 2


def row(event_id: str, day: str, amount: int, currency: str = "COP", owner: str = "santiago") -> dict:
    return {"id": event_id, "day": day, "amount_minor": amount, "currency": currency, "owner": owner}


def test_reconcile_matches_known_movements_once_and_from_the_start_date():
    card = st.parse("TARJETA_MASTERCARD", "202601", CARD)
    rows = [row("a", "2026-01-05", 4500000), row("b", "2026-01-04", 4500000), row("m", "2026-01-03", 1234, "USD", "manuela")]
    report = st.reconcile([card], rows, date(2026, 1, 1))
    assert [(c.movement.description, matched) for c, matched in report.matched] == [("TIENDA FICTICIA", "b")]
    assert [c.movement.description for c in report.missing] == ["CUOTA DE MANEJO", "ABONO SUCURSAL VIRTUAL", "SERVICIO FICTICIO"]
    assert st.reconcile([card], rows, date(2026, 1, 7)).missing[0].movement.description == "SERVICIO FICTICIO"
    check = report.checks()[0]
    assert (check["movements"], check["matched"], check["missing"]) == (4, 1, 3)


def test_statement_movements_enter_pending_audited_and_only_once(store):
    card = st.parse("TARJETA_MASTERCARD", "202601", CARD)
    report = st.reconcile([card], [], date(2026, 1, 1))
    events = [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing]
    assert store.record_statements(report.checks(), events) == 4
    assert store.record_statements(report.checks(), events) == 0
    rows = store.state()["transactions"]
    assert {r["decision"] for r in rows} == {None}
    assert {r["kind"] for r in rows} == {"purchase", "card_payment"}
    assert store.state()["statement_checks"][0]["missing"] == 4
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='statement-import'").fetchone()[0] == 4
    with pytest.raises(ValueError, match="pendiente"):
        store.record_statements([], [{**events[0], "id": "stmt:x", "decision": "personal"}])


def test_merchant_rules_never_decide_statement_movements(store):
    store.add_rule("TIENDA FICTICIA", "Compras", "personal")
    card = st.parse("TARJETA_MASTERCARD", "202601", CARD)
    report = st.reconcile([card], [], date(2026, 1, 1))
    store.record_statements(report.checks(), [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing])
    store.apply_rules()  # e.g. a later notice import runs every rule again
    assert {r["decision"] for r in store.state()["transactions"]} == {None}


def test_loan_instalment_counts_only_as_personal_family_or_nothing(store):
    mortgage = st.parse("HIPOTECARIO", "202601", MORTGAGE)
    report = st.reconcile([mortgage], [], date(2026, 1, 1))
    store.record_statements(report.checks(), [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing])
    loan = store.state()["transactions"][0]
    with pytest.raises(ValueError, match="cuota"):
        store.decide(loan["id"], "investment")
    store.decide(loan["id"], "family")
    assert store.monthly_totals("santiago")["2026-01"]["family"] == 150000000
    assert store.state()["statement_checks"][0]["balance_minor"] == 15000000000


def test_encrypted_pdf_is_read_in_memory_with_the_password():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("clave-ficticia", algorithm="AES-256")
    buffer = io.BytesIO()
    writer.write(buffer)
    assert st.decrypt_text(buffer.getvalue(), "clave-ficticia") == ""
    with pytest.raises(st.StatementError, match="contrase"):
        st.decrypt_text(buffer.getvalue(), "otra")


class FakeClient:
    def __init__(self, sender: str):
        self.sender = sender

    def get_json(self, url, params=None):
        if url.endswith("/me/messages"):
            assert "from/emailAddress/address eq 'banco@example.test'" in params["$filter"]
            return {"value": [{"id": "m1", "from": {"emailAddress": {"address": self.sender}}, "receivedDateTime": "2026-02-01T00:00:00Z"}]}
        if url.endswith("/attachments"):
            return {"value": [
                {"id": "a1", "name": "Extracto_1_202601_TARJETA_MASTERCARD_0002.pdf", "size": 10},
                {"id": "a2", "name": "foto.png", "size": 10},
            ]}
        return {"@odata.type": "#microsoft.graph.fileAttachment", "contentBytes": base64.b64encode(b"%PDF").decode()}


def test_statement_files_only_come_from_the_approved_sender():
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reader = GraphSourceReader(FakeClient("banco@example.test"), ["avisos@example.test"])
    files = list(reader.statement_files("banco@example.test", since, st.FILE_NAME, st.MAX_PDF_BYTES))
    assert files == [("Extracto_1_202601_TARJETA_MASTERCARD_0002.pdf", b"%PDF")]
    other = GraphSourceReader(FakeClient("otro@example.test"), ["avisos@example.test"])
    with pytest.raises(OutlookError, match="otro remitente"):
        list(other.statement_files("banco@example.test", since, st.FILE_NAME, st.MAX_PDF_BYTES))


def run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only):
    (tmp_path / "statements.json").write_text(json.dumps({
        "password": "clave-ficticia", "approved_senders": ["banco@example.test"],
    }), encoding="utf-8")

    class Reader:
        def statement_files(self, sender, since, pattern, limit):
            for name in texts:
                yield name, name.encode()

    @contextmanager
    def fake_source(*args):
        yield Reader()

    monkeypatch.setattr(app, "open_source", fake_source)
    monkeypatch.setattr(st, "decrypt_text", lambda content, password: texts[content.decode()])
    return app.reconcile_statements(store, tmp_path, {}, preferences, date(2026, 1, 1), read_only)


def test_reconcile_command_reports_read_only_then_imports(store, tmp_path, preferences, monkeypatch):
    texts = {
        "Extracto_1_202601_TARJETA_MASTERCARD_0002.pdf": CARD,
        "Extracto_1_202601_FIDUCUENTA_0003.pdf": FUND,
        "Extracto_1_202601_CONSOLIDADO_0003.pdf": "no se lee",
    }
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=True)
    assert result == {"statements": 2, "matched": 0, "missing": 5, "imported": 0, "balances": 1}
    assert store.state()["transactions"] == [] and store.state()["statement_checks"] == []
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)
    assert result["imported"] == 5
    assert {r["account"] for r in store.state()["transactions"]} == {"Tarjeta de crédito Master", "Fiducuenta"}


def test_unreadable_statement_stops_without_importing(store, tmp_path, preferences, monkeypatch):
    texts = {
        "Extracto_1_202601_TARJETA_MASTERCARD_0002.pdf": CARD,
        "Extracto_1_202602_CREDITO_4444.pdf": "formato desconocido",
    }
    with pytest.raises(ValueError, match="CREDITO 202602"):
        run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)
    assert store.state()["transactions"] == []


def test_statement_settings_require_password_and_exact_senders(tmp_path):
    with pytest.raises(ValueError, match="Falta"):
        app.statement_settings(tmp_path)
    (tmp_path / "statements.json").write_text(json.dumps({"password": "", "approved_senders": ["x"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="password") as error:
        app.statement_settings(tmp_path)
    assert "clave" not in str(error.value)


def test_panel_shows_statement_checks_and_balances(store):
    from playwright.sync_api import sync_playwright
    from monthly_close.server import create_server

    statements = [st.parse("FIDUCUENTA", "202601", FUND), st.parse("HIPOTECARIO", "202601", MORTGAGE)]
    report = st.reconcile(statements, [], date(2026, 1, 1))
    store.record_statements(report.checks(), [])
    server = create_server(store, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/?month=2026-01&currency=COP&owner=santiago")
            page.wait_for_load_state("networkidle")
            text = page.locator("#statements-card").inner_text()
            assert "Inversiones" in text and "Créditos" in text
            assert re.search(r"1[.\s]?204[.\s]?950,00", text)
            assert "Fiducuenta *0003" in text
            page.locator("#owner-switch").get_by_role("button", name="Manuela").click()
            assert page.locator("#statements-card").is_hidden()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_payroll_deducted_instalment_adds_matching_payroll_income(store):
    credit = st.parse("CREDITO", "202602", CREDIT)
    report = st.reconcile([credit], [], date(2026, 1, 1))
    store.record_statements(report.checks(), [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing])
    loan = store.state()["transactions"][0]
    assert store.set_payroll_deduction("santiago", "4444") == 0  # pending instalment: nothing yet
    store.decide(loan["id"], "family")
    twins = [r for r in store.state()["transactions"] if r["id"].startswith("payroll:")]
    assert [(t["decision"], t["category"], t["amount_minor"], t["day"]) for t in twins] == [
        ("income", "Nómina", loan["amount_minor"], loan["day"]),
    ]
    totals = store.monthly_totals("santiago")["2026-02"]
    assert totals["income"] - totals["net"] == 0  # the deduction no longer counts twice
    store.decide(loan["id"], "family")  # deciding again does not duplicate it
    assert sum(r["id"].startswith("payroll:") for r in store.state()["transactions"]) == 1
    store.decide(loan["id"], "exclude")
    assert not any(r["id"].startswith("payroll:") for r in store.state()["transactions"])
    with store.connect() as db:
        actions = [row[0] for row in db.execute("SELECT value FROM audit WHERE action='payroll-deduction-income'")]
    assert actions == ["added", "removed"]
    with pytest.raises(ValueError):
        store.set_payroll_deduction("santiago", "44a4")


def test_payroll_deduction_applies_to_already_decided_instalments(store):
    credit = st.parse("CREDITO", "202602", CREDIT)
    report = st.reconcile([credit], [], date(2026, 1, 1))
    store.record_statements(report.checks(), [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing])
    store.decide(store.state()["transactions"][0]["id"], "personal")
    assert store.set_payroll_deduction("santiago", "4444") == 1
    assert store.set_payroll_deduction("santiago", "4444") == 0
