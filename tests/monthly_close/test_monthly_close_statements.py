"""Bank statements: parsing, reconciliation and import, with fictitious text only."""
from __future__ import annotations

import base64
import io
import json
import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from playwright.sync_api import expect
from pypdf import PdfWriter

from builders import statement_line
from monthly_close import main as app
from monthly_close import statements as st
from monthly_close.domain import Preferences
from monthly_close.extraction import Mail, extract
from monthly_close.gmail_reader import GmailSourceReader
from monthly_close.graph_reader import GraphSourceReader
from monthly_close.outlook import OutlookError
from monthly_close.rule_suggestions import propose_statement_categories
from monthly_close.store import DUPLICATE_NOTE, Store
from shared.local_model import LocalModelError

SAVINGS = """Extracto de cuenta
DESDE: 2026/01/01 HASTA: 2026/03/31
CUENTA DE AHORROS
NÚMERO 12345670001
SALDO ANTERIOR $ 1,000,000.00 SALDO PROMEDIO $ 900,000
TOTAL ABONOS $ 300,000.25 CUENTAS X COBRAR $ .00
TOTAL CARGOS $ 350,000.00 VALOR INTERESES PAGADOS $ .25
SALDO ACTUAL $ 950,000.25 RETEFUENTE $ .00
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
def preferences(make_preferences) -> Preferences:
    return make_preferences()  # no excluded accounts


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


def test_savings_statement_with_lines_left_out_stops():
    """The real case: from page two on, rows were not read and nothing complained."""
    last = "10/03 TRANSFERENCIA DESDE PERSONA FICTICIA 300,000.00 950,000.25\n"
    with pytest.raises(st.StatementError, match="no cuadran con su resumen"):
        st.parse("CTA_AHORROS", "202603", SAVINGS.replace(last, ""))
    with pytest.raises(st.StatementError, match="resumen de saldos"):
        st.parse("CTA_AHORROS", "202603", SAVINGS.replace("SALDO ACTUAL", "SALDO"))


def test_only_the_savings_statement_is_read_in_layout_mode(monkeypatch, tmp_path, store, preferences):
    modes = []
    monkeypatch.setattr(st, "parse", lambda product, period, text: (_ for _ in ()).throw(st.StatementError("x")))

    def decrypt(content, password, layout=False):
        modes.append((content.decode(), layout))
        return ""

    monkeypatch.setattr(st, "decrypt_text", decrypt)
    box = app.load_mailboxes({})[0]
    for name in ("Extracto_1_202603_CTA_AHORROS_0001.pdf", "Extracto_1_202603_TARJETA_AMEX_0002.pdf"):
        with pytest.raises(ValueError):
            app._read_statement(name, name.encode(), {"santiago": "clave"}, date(2026, 1, 1), box)
    assert modes == [("Extracto_1_202603_CTA_AHORROS_0001.pdf", True), ("Extracto_1_202603_TARJETA_AMEX_0002.pdf", False)]


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
    # Only the card's own-payment wording is decided (No cuenta); everything else waits.
    assert {(r["merchant"], r["decision"]) for r in rows if r["decision"] is not None} == {
        ("ABONO SUCURSAL VIRTUAL", "exclude"),
    }
    assert {r["kind"] for r in rows} == {"purchase", "card_payment"}
    assert store.state()["statement_checks"][0]["missing"] == 4
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='statement-import'").fetchone()[0] == 4
    with pytest.raises(ValueError, match="pendiente"):
        store.record_statements([], [{**events[0], "id": "stmt:x", "decision": "personal"}])


def test_the_persons_rules_decide_statement_movements_except_possible_duplicates(store):
    store.add_rule("TIENDA FICTICIA", "Compras", "personal")
    card = st.parse("TARJETA_MASTERCARD", "202601", CARD)
    report = st.reconcile([card], [], date(2026, 1, 1))
    events = [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing]
    store.record_statements(report.checks(), events)
    rows = [r for r in store.state()["transactions"] if "TIENDA FICTICIA" in r["merchant"]]
    assert rows and {(r["decision"], r["category"]) for r in rows} == {("personal", "Compras")}
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='decision-rule'").fetchone()[0] == len(rows)


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


class FakeGmailStatements:
    """Gmail answers for one statement e-mail; the PDF sits two levels deep."""

    def __init__(self, sender: str, size: int = 4, data: object = "JVBERg"):
        self.sender, self.size, self.data, self.urls = sender, size, data, []

    def get_json(self, url, params=None):
        self.urls.append(url)
        if url.endswith("/messages"):
            assert params["q"].startswith('from:("banco@example.test") has:attachment after:')
            return {"messages": [{"id": "g1"}]}
        if url.endswith("/attachments/att-1"):
            return {"data": self.data}
        assert params["format"] == "full" and "data" not in params["fields"]  # never the message text
        return {"id": "g1", "payload": {
            "headers": [{"name": "From", "value": f"Banco <{self.sender}>"}],
            "parts": [{"filename": "", "body": {"size": 10}, "parts": [
                {"filename": "Extracto_1_202601_TARJETA_VISA_0002.pdf", "body": {"attachmentId": "att-1", "size": self.size}},
                {"filename": "logo.png", "body": {"attachmentId": "att-2", "size": 10}},
            ]}],
        }}


def test_gmail_statement_files_only_come_from_the_approved_sender():
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = FakeGmailStatements("banco@example.test")
    reader = GmailSourceReader(client, ["avisos@example.test"])
    files = list(reader.statement_files("banco@example.test", since, st.STATEMENT_NAME, st.MAX_PDF_BYTES))
    assert files == [("Extracto_1_202601_TARJETA_VISA_0002.pdf", b"%PDF")]
    assert not any(url.endswith("att-2") for url in client.urls)  # other attachments are not downloaded
    other = GmailSourceReader(FakeGmailStatements("otro@example.test"), ["avisos@example.test"])
    with pytest.raises(OutlookError, match="otro remitente"):
        list(other.statement_files("banco@example.test", since, st.STATEMENT_NAME, st.MAX_PDF_BYTES))
    large = GmailSourceReader(FakeGmailStatements("banco@example.test", size=st.MAX_PDF_BYTES + 1), ["avisos@example.test"])
    with pytest.raises(OutlookError, match="tamano"):
        list(large.statement_files("banco@example.test", since, st.STATEMENT_NAME, st.MAX_PDF_BYTES))


@pytest.mark.parametrize(("sender", "since", "data", "limit", "error", "message"), [
    ('banco"@example.test', datetime(2026, 1, 1, tzinfo=timezone.utc), "JVBERg", 10, ValueError, "Remitente"),
    ("banco@example.test", datetime(2026, 1, 1), "JVBERg", 10, ValueError, "zona horaria"),
    ("banco@example.test", datetime(2026, 1, 1, tzinfo=timezone.utc), None, 10, OutlookError, "no reconocido"),
    ("banco@example.test", datetime(2026, 1, 1, tzinfo=timezone.utc), "J", 10, OutlookError, "no valido"),
    # The declared size fits, the downloaded content does not.
    ("banco@example.test", datetime(2026, 1, 1, tzinfo=timezone.utc), "JVBERi0xLjcK", 4, OutlookError, "tamano"),
])
def test_gmail_statement_files_refuse_what_cannot_be_verified(sender, since, data, limit, error, message):
    reader = GmailSourceReader(FakeGmailStatements("banco@example.test", data=data), ["avisos@example.test"])
    with pytest.raises(error, match=message):
        list(reader.statement_files(sender, since, st.STATEMENT_NAME, limit))


TWO_MAILBOXES = {"mailboxes": [
    {"name": "santiago", "provider": "graph", "owner": "santiago"},
    {"name": "manuela", "provider": "gmail", "owner": "manuela", "verified": True},
]}


def run_reconcile(
    store, tmp_path, preferences, monkeypatch, texts, read_only, settings=None, by_mailbox=None, opens=None,
    tried=None,
):
    """Reconcile with fake mailboxes; by_mailbox gives each mailbox its own attachment names.

    opens maps an attachment name to the only password that opens it; tried records
    which password each attachment was opened with, when given.
    """
    (tmp_path / "statements.json").write_text(json.dumps({
        "passwords": {"santiago": "clave-santiago", "manuela": "clave-manuela"},
        "approved_senders": ["banco@example.test"],
    }), encoding="utf-8")
    tried = [] if tried is None else tried

    class Reader:
        def __init__(self, names):
            self.names = names

        def statement_files(self, sender, since, pattern, limit):
            for name in self.names:
                yield name, name.encode()

    @contextmanager
    def fake_source(data_dir, settings, senders, box=None):
        yield Reader(list(texts) if by_mailbox is None else by_mailbox.get(box.name, []))

    monkeypatch.setattr(app, "open_source", fake_source)
    def decrypt(content, password, layout=False):
        name = content.decode()
        if opens and opens.get(name) != password:
            raise st.StatementError("La contraseña configurada no abre el extracto.")
        tried.append((name, password))
        return texts[name]

    monkeypatch.setattr(st, "decrypt_text", decrypt)
    # Tests never reach a real Ollama: this one recognises nothing, so only rules and
    # past decisions propose categories.
    class SilentModel:
        def __init__(self, url, model):
            pass

        def suggest_categories(self, merchants, categories, examples=None):
            return {"suggestions": []}

    monkeypatch.setattr(app, "OllamaJsonModel", SilentModel)
    settings = {"ollama_url": "http://127.0.0.1:11434", "ollama_model": "prueba", **(settings or {})}
    return app.reconcile_statements(store, tmp_path, settings, preferences, date(2026, 1, 1), read_only)


def test_each_mailbox_statements_reconcile_against_its_owners_movements(store, tmp_path, preferences, monkeypatch):
    store.import_message("m-1", [{
        "id": "visa-purchase", "message_id": "m-1", "day": "2026-01-03", "merchant": "TIENDA FICTICIA",
        "account": "Tarjeta A", "amount_minor": 4500000, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": "personal", "reason": "sintetico",
    }], owner="manuela")
    texts = {"Extracto_1_202601_TARJETA_VISA_0002.pdf": CARD, "Extracto_1_202601_FIDUCUENTA_0003.pdf": FUND}
    tried = []
    result = run_reconcile(
        store, tmp_path, preferences, monkeypatch, texts, read_only=False, settings=TWO_MAILBOXES,
        by_mailbox={"santiago": ["Extracto_1_202601_FIDUCUENTA_0003.pdf"], "manuela": ["Extracto_1_202601_TARJETA_VISA_0002.pdf"]},
        tried=tried,
    )
    assert result["statements"] == 2 and result["matched"] == 1
    # Each mailbox's statements are opened only with its owner's password.
    assert sorted(tried) == [
        ("Extracto_1_202601_FIDUCUENTA_0003.pdf", "clave-santiago"),
        ("Extracto_1_202601_TARJETA_VISA_0002.pdf", "clave-manuela"),
    ]
    checks = {row["product"]: row["owner"] for row in store.state()["statement_checks"]}
    assert checks == {"TARJETA_VISA": "manuela", "FIDUCUENTA": "santiago"}
    owners = {row["account"]: row["owner"] for row in store.state()["transactions"] if row["id"].startswith("stmt:")}
    assert owners == {"Tarjeta de crédito Visa": "manuela", "Fiducuenta": "santiago"}


def test_a_statement_in_two_owners_mailboxes_stops_without_importing(store, tmp_path, preferences, monkeypatch):
    texts = {"Extracto_1_202601_TARJETA_VISA_0002.pdf": CARD}
    with pytest.raises(ValueError, match="buzones de santiago y manuela"):
        run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False, settings=TWO_MAILBOXES)
    assert store.state()["transactions"] == [] and store.state()["statement_checks"] == []


def test_a_statement_that_does_not_open_with_its_owners_password_stops(store, tmp_path, preferences, monkeypatch):
    """A statement of Santiago that reaches Manuela's mailbox is never counted as hers."""
    name = "Extracto_1_202601_TARJETA_VISA_0002.pdf"
    with pytest.raises(ValueError, match="buzon manuela: .*Se probo solo la de manuela"):
        run_reconcile(
            store, tmp_path, preferences, monkeypatch, {name: CARD}, read_only=False,
            settings=TWO_MAILBOXES, by_mailbox={"manuela": [name]}, opens={name: "clave-santiago"},
        )
    assert store.state()["transactions"] == [] and store.state()["statement_checks"] == []


def test_a_mailbox_without_its_owners_password_stops(store, tmp_path, preferences, monkeypatch):
    monkeypatch.setattr(app, "statement_settings", lambda data_dir: {
        "passwords": {"santiago": "clave-santiago"}, "approved_senders": ["banco@example.test"],
    })
    with pytest.raises(ValueError, match="Falta la contrasena de los extractos de manuela"):
        run_reconcile(
            store, tmp_path, preferences, monkeypatch, {"Extracto_1_202601_TARJETA_VISA_0002.pdf": CARD},
            read_only=False, settings=TWO_MAILBOXES,
            by_mailbox={"manuela": ["Extracto_1_202601_TARJETA_VISA_0002.pdf"]},
        )


def test_each_owners_proteccion_statement_is_kept_apart(store, tmp_path, preferences, monkeypatch):
    """Protección statements carry no product number; the mailbox tells whose they are."""
    texts = {"123_PensionesCesantias_202606.pdf": SEVERANCE}
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False, settings=TWO_MAILBOXES)
    assert result["statements"] == 2
    checks = sorted((row["owner"], row["identity"]) for row in store.state()["statement_checks"])
    assert [owner for owner, _ in checks] == ["manuela", "santiago"]
    assert checks[0][1] == checks[1][1] + ":manuela"


def test_reconcile_command_reports_read_only_then_imports(store, tmp_path, preferences, monkeypatch):
    texts = {
        "Extracto_1_202601_TARJETA_MASTERCARD_0002.pdf": CARD,
        "Extracto_1_202601_FIDUCUENTA_0003.pdf": FUND,
        "Extracto_1_202601_CONSOLIDADO_0003.pdf": "no se lee",
    }
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=True)
    assert result == {"statements": 2, "matched": 0, "missing": 5, "imported": 0, "balances": 1, "reclassified": 0, "categories_proposed": 0}
    assert store.state()["transactions"] == [] and store.state()["statement_checks"] == []
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)
    assert result["imported"] == 5
    assert {r["account"] for r in store.state()["transactions"]} == {"Tarjeta de crédito Master", "Fiducuenta"}


def test_unreadable_statement_stops_without_importing(store, tmp_path, preferences, monkeypatch):
    texts = {
        "Extracto_1_202601_TARJETA_MASTERCARD_0002.pdf": CARD,
        "Extracto_1_202602_CREDITO_4444.pdf": "formato desconocido",
    }
    with pytest.raises(ValueError, match="CREDITO 202602 del buzon santiago"):
        run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)
    assert store.state()["transactions"] == []


def test_statement_settings_require_password_and_exact_senders(tmp_path):
    with pytest.raises(ValueError, match="Falta"):
        app.statement_settings(tmp_path)
    (tmp_path / "statements.json").write_text(json.dumps({"password": "", "approved_senders": ["x"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="password") as error:
        app.statement_settings(tmp_path)
    assert "clave" not in str(error.value)
    # The older single password is Santiago's.
    (tmp_path / "statements.json").write_text(json.dumps({
        "password": "clave-a", "passwords": {"manuela": "clave-b"}, "approved_senders": ["Banco@Example.test"],
    }), encoding="utf-8")
    assert app.statement_settings(tmp_path) == {
        "passwords": {"santiago": "clave-a", "manuela": "clave-b"}, "approved_senders": ["banco@example.test"],
    }
    for passwords in (["clave-a"], {"otra": "clave"}, {"manuela": 7}, {"manuela": ""}):
        (tmp_path / "statements.json").write_text(json.dumps({
            "passwords": passwords, "approved_senders": ["banco@example.test"],
        }), encoding="utf-8")
        with pytest.raises(ValueError, match="passwords"):
            app.statement_settings(tmp_path)
    (tmp_path / "statements.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="objeto"):
        app.statement_settings(tmp_path)


def test_panel_shows_statement_checks_and_balances(store, start_panel, new_page):
    statements = [st.parse("FIDUCUENTA", "202601", FUND), st.parse("HIPOTECARIO", "202601", MORTGAGE)]
    visa = st.parse("TARJETA_VISA", "202601", CARD)
    visa.owner = "manuela"
    report = st.reconcile(statements + [visa], [], date(2026, 1, 1))
    store.record_statements(report.checks(), [])
    url = start_panel(store)
    page = new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{url}/?month=2026-01&currency=COP&owner=santiago")
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    text = page.locator("#statements-card").inner_text()
    assert "Inversiones" in text and "Créditos" in text
    assert re.search(r"1[.\s]?204[.\s]?950,00", text)
    assert "Fiducuenta *0003" in text and "Visa" not in text
    page.locator("#owner-switch").get_by_role("button", name="Manuela").click()
    text = page.locator("#statements-card").inner_text()
    assert "Visa *0002" in text and "Fiducuenta" not in text
    page.locator("#owner-switch").get_by_role("button", name="Hogar").click()
    card = page.locator("#statements-card")
    text = card.inner_text()
    assert "Tarjetas y cuentas" in text and "Inversiones" in text and "Créditos" in text
    # In the household view each statement carries its owner's tag.
    for name, owner in (("Visa *0002", "Manuela"), ("Fiducuenta *0003", "Santiago")):
        expect(card.locator(".statement-name").filter(has_text=name).locator(".owner-tag")).to_have_text(owner)
    assert "0/0" not in text
    assert not errors


def test_year_view_shows_how_investments_and_credits_moved(store, start_panel, new_page):
    # Month-end balances: a fund that grows, a credit that goes down, and one of Manuela's.
    balances = [
        ("santiago", "FIDUCUENTA", "0003", "2025-12-31", 100_000_00), ("santiago", "FIDUCUENTA", "0003", "2026-02-28", 130_000_00),
        ("santiago", "CREDITO", "0004", "2026-01-31", 900_000_00), ("santiago", "CREDITO", "0004", "2026-03-31", 850_000_00),
        ("manuela", "FIDUCUENTA", "0005", "2026-02-28", 50_000_00),
        # A product with no statement in the year is left out.
        ("santiago", "CREDITO", "0006", "2025-06-30", 10_000_00),
    ]
    with store.connect() as db:
        for owner, product, suffix, end, balance in balances:
            db.execute(
                """INSERT INTO statement_checks (identity,owner,product,suffix,period_start,period_end,movements,
                matched,missing,balance_minor,checked_at) VALUES (?,?,?,?,?,?,0,0,0,?,?)""",
                (f"{owner}{product}{suffix}{end}", owner, product, suffix, end[:8] + "01", end, balance, "2026-04-01T00:00:00"),
            )
    url = start_panel(store)
    page = new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{url}/?tab=year&year=2026&owner=hogar")
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    investments, credits = page.locator("#investment-balances"), page.locator("#credit-balances")
    expect(page.locator("#balances-card")).to_be_visible()
    expect(investments.locator(".statement-head strong")).to_have_text(re.compile(r"180[.\s]?000,00"))
    expect(credits.locator(".statement-head strong")).to_have_text(re.compile(r"850[.\s]?000,00"))
    expect(credits.locator(".statement-head p")).to_contain_text(re.compile(r"bajó 50[.\s]?000,00"))
    assert "*0006" not in credits.inner_text()
    fund = investments.locator(".statement-row").filter(has_text="*0003")
    expect(fund).to_contain_text(re.compile(r"subió 30[.\s]?000,00\sCOP desde el inicio del año"))
    # One point per month up to the latest, carrying the last known balance forward.
    tips = investments.locator("svg rect[data-tip]").evaluate_all("rects => rects.map(rect => rect.dataset.tip)")
    assert tips[0].startswith("Enero de 2026: 100") and "1 producto" in tips[0]
    assert re.search(r"Marzo de 2026: 180[.\s]?000,00\sCOP · 2 productos", tips[2])
    page.locator("#owner-switch").get_by_role("button", name="Santiago").click()
    assert "*0005" not in investments.inner_text()
    assert not errors

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


def test_only_the_ordinary_instalment_counts_as_payroll_and_the_extra_waits(store):
    january = st.parse("CREDITO", "202601", CREDIT.replace("2/05/2026", "1/05/2026").replace(
        "TOTAL 450,000.00 451,000.00", "TOTAL 400,000.00 400,000.00"))
    june = st.parse("CREDITO", "202602", CREDIT.replace(
        "TOTAL 450,000.00 451,000.00", "TOTAL 1,900,000.00 400,000.00"))
    report = st.reconcile([january, june], [], date(2026, 1, 1))
    store.record_statements(report.checks(), [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing])
    store.set_payroll_deduction("santiago", "4444")
    loans = sorted((r for r in store.state()["transactions"] if r["kind"] == "loan"), key=lambda r: r["day"])
    for loan in loans:
        store.decide(loan["id"], "family")
    rows = {r["id"]: r for r in store.state()["transactions"]}
    big = loans[1]
    assert (rows["payroll:" + big["id"]]["amount_minor"], rows["payroll:" + big["id"]]["decision"]) == (40000000, "income")
    extra = rows["payroll-extra:" + big["id"]]
    assert (extra["amount_minor"], extra["decision"], extra["kind"]) == (150000000, None, "income")
    assert "payroll-extra:" + loans[0]["id"] not in rows  # paid exactly the instalment
    store.decide(extra["id"], "exclude")  # it was paid from an account
    store.decide(big["id"], "family")  # syncing again keeps the person's choice
    assert {r["id"]: r for r in store.state()["transactions"]}[extra["id"]]["decision"] == "exclude"
    store.decide(big["id"], "exclude")
    assert not any(r["id"].endswith(big["id"]) and r["id"] != big["id"] for r in store.state()["transactions"])


# ---------------------------------------------------------------- Protección (fictitious text)

VOLUNTARY_CURRENT = """Hola Persona Ficticia
Esto pasó en el último período de tu Fondo de Pensiones Voluntarias.
Datos personales
Persona Ficticia
CC 0000000000
persona@example.test
Datos del extracto
Período: 30 de Septiembre 2026 a
31 de Octubre 2026
Fecha expedición: Noviembre 8 de 2026
Saldo que tenías a corte del 30 de Septiembre 2026 Saldo inicial del período:$ 10,000,000
Aportes
El dinero que ingresó a tu cuenta. +$ 500,000
Retiros
Dinero que sacaste de tu cuenta. -$ 0
Traslados a otras entidades
Movimientos que realizaste hacia otras entidades. -$ 0
Rendimiento neto
Corresponde al rendimiento de tu inversión después de descontar el
costo de administración del mes (0.10%).
-$ 200,003
Devolución de costo administración
Tu beneficio por mantener una inversión constante con Protección. +$ 20,000
Saldo a corte 31 de Octubre 2026
Saldo final del período
$ 10,320,000
Aportes $ 500,000
"""
VOLUNTARY_PREVIOUS = """Llevas 30 meses con este fondo
En este tiempo has invertido $ 9,000,000
Rendimiento bruto $ 950,000
Rendimiento neto $ 900,000
1. Resumen de tu cuenta y período
Resumen de tu cuenta
Resumen de este período
Saldo inicial
Diciembre 31 de 2025
$ 10,000,000
Enero 31 de 2026
Saldo final
$ 11,320,000
Fecha afiliación
2020/01/01 Tu período
Aportes
$ 400,000
Retiros Gastos y comisiones
$ 0 -$ 50,000
Datos del extracto
Período: Diciembre 31 de 2025  a  Enero 31 de 2026
Traslados de recursos de/hacia fondos de pensiones  $ 0
Aportes  $ 0
Retiros  $ 0
Rendimientos  $ 0
Aportes $ 400,000
Rendimientos $ 900,000
Devolución de comisión $ 20,000
Gastos y comisiones -$ 50,000
"""
SEVERANCE = """EXTRACTO FONDO DE CESANTÍAS PROTECCIÓN
Saldo del periodo comprendido entre: 2026/04/01 a 2026/09/30
Saldo Inicial
Aportes
Retiro
Valorización
Saldo Final
$ 20.000.000 $ 1.000.000 $ 0 $ -99.999 $ 20.900.001
"""


def test_protection_attachment_names_and_statement_mails_are_recognised():
    assert st.identify("0A81F6F5012E_PensionVoluntaria_202610.pdf") == ("PROTECCION_VOLUNTARIA", "202610")
    assert st.identify("0A81F716DA_PensionesCesantias_202609.pdf") == ("PROTECCION_CESANTIAS", "202609")
    assert st.identify("Extracto_1_202601_FIDUCUENTA_0003.pdf") == ("FIDUCUENTA", "202601")
    assert st.identify("PensionVoluntaria_202610.pdf") is None and st.identify("otro.pdf") is None
    assert all(st.STATEMENT_NAME.fullmatch(name) for name in (
        "0A81F6F5012E_PensionVoluntaria_202610.pdf", "Extracto_1_202601_FIDUCUENTA_0003.pdf",
    ))
    for subject in (
        "Extracto Pensiones Voluntarias Protección S.A.", "Extracto Pensiones Voluntarias Proteccion S.A",
        "Extracto de Cesantias Protección.",
    ):
        assert st.is_statement_notice("AFP_Proteccion@proteccion.com", subject)
    assert not st.is_statement_notice("afp_proteccion@proteccion.com", "Recibimos tu aporte")
    assert not st.is_statement_notice("otro@example.test", "Extracto Pensiones Voluntarias Protección S.A.")


def test_current_voluntary_statement_keeps_balance_and_period_figures():
    statement = st.parse("PROTECCION_VOLUNTARIA", "202610", VOLUNTARY_CURRENT)
    assert (statement.product, statement.suffix) == ("PROTECCION_VOLUNTARIA", "")
    assert (statement.period_start, statement.period_end) == (date(2026, 9, 30), date(2026, 10, 31))
    # Printed figures are rounded to whole pesos; the stored returns absorb the gap.
    assert (statement.opening_minor, statement.contributions_minor, statement.withdrawals_minor) == (
        1_000_000_000, 50_000_000, 0,
    )
    assert (statement.returns_minor, statement.balance_minor) == (-18_000_000, 1_032_000_000)
    assert statement.movements == []  # figures only: nothing to import


def test_previous_voluntary_format_reads_the_labelled_detail_not_the_empty_template():
    statement = st.parse("PROTECCION_VOLUNTARIA", "202601", VOLUNTARY_PREVIOUS)
    assert (statement.period_start, statement.period_end) == (date(2025, 12, 31), date(2026, 1, 31))
    assert (
        statement.opening_minor, statement.contributions_minor, statement.withdrawals_minor,
        statement.returns_minor, statement.balance_minor,
    ) == (1_000_000_000, 40_000_000, 0, 92_000_000, 1_132_000_000)


def test_severance_statement_reads_its_summary_including_a_negative_valuation():
    statement = st.parse("PROTECCION_CESANTIAS", "202609", SEVERANCE)
    assert (statement.period_start, statement.period_end) == (date(2026, 4, 1), date(2026, 9, 30))
    assert (
        statement.opening_minor, statement.contributions_minor, statement.withdrawals_minor,
        statement.returns_minor, statement.balance_minor,
    ) == (2_000_000_000, 100_000_000, 0, -9_999_900, 2_090_000_100)


@pytest.mark.parametrize("product,text,old,new", [
    ("PROTECCION_VOLUNTARIA", VOLUNTARY_CURRENT, "$ 10,320,000\nAportes", "$ 10,320,011\nAportes"),
    ("PROTECCION_VOLUNTARIA", VOLUNTARY_PREVIOUS, "Saldo final\n$ 11,320,000", "Saldo final\n$ 11,321,000"),
    ("PROTECCION_CESANTIAS", SEVERANCE, "$ 20.900.001", "$ 20.900.050"),
])
def test_protection_figures_that_do_not_add_up_beyond_rounding_are_refused(product, text, old, new):
    assert old in text
    with pytest.raises(st.StatementError, match="no cuadra"):
        st.parse(product, "202610", text.replace(old, new))


@pytest.mark.parametrize("product,text,missing", [
    ("PROTECCION_VOLUNTARIA", VOLUNTARY_CURRENT, "Saldo final del período\n"),
    ("PROTECCION_VOLUNTARIA", VOLUNTARY_PREVIOUS, "Rendimiento neto $ 900,000\n"),
    ("PROTECCION_VOLUNTARIA", VOLUNTARY_CURRENT, "Período: 30 de Septiembre 2026 a\n"),
    ("PROTECCION_CESANTIAS", SEVERANCE, "Saldo del periodo comprendido entre: 2026/04/01 a 2026/09/30\n"),
])
def test_incomplete_protection_statements_are_refused(product, text, missing):
    assert missing in text
    with pytest.raises(st.StatementError):
        st.parse(product, "202610", text.replace(missing, ""))


def test_reconcile_stores_protection_figures_without_importing_movements(store, tmp_path, preferences, monkeypatch):
    texts = {
        "0A81F6F5_PensionVoluntaria_202610.pdf": VOLUNTARY_CURRENT,
        "0A81F716_PensionesCesantias_202609.pdf": SEVERANCE,
        "0A81F6F5_PensionVoluntaria_202512.pdf": VOLUNTARY_PREVIOUS,  # before the start: skipped
    }
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)
    assert result == {"statements": 2, "matched": 0, "missing": 0, "imported": 0, "balances": 2, "reclassified": 0, "categories_proposed": 0}
    assert store.state()["transactions"] == []
    checks = {check["product"]: check for check in store.state()["statement_checks"]}
    assert checks["PROTECCION_VOLUNTARIA"]["contributions_minor"] == 50_000_000
    assert checks["PROTECCION_VOLUNTARIA"]["returns_minor"] == -18_000_000
    assert checks["PROTECCION_CESANTIAS"]["balance_minor"] == 2_090_000_100
    # Repeating it changes nothing.
    run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)
    assert len(store.state()["statement_checks"]) == 2


def test_a_protection_statement_mail_is_never_sent_to_ollama(preferences):

    class NoModel:
        def complete_json(self, system, user):
            raise AssertionError("A statement e-mail must not reach Ollama.")

    own = replace(preferences, approved_senders=["afp_proteccion@proteccion.com"])
    mail = Mail(
        "id", "afp_proteccion@proteccion.com", "Extracto Pensiones Voluntarias Protección S.A.",
        "2026-03-05T23:00:51+00:00", "Adjuntamos tu extracto. Saldo $ 1.234.567",
    )
    assert extract(mail, own, NoModel()) == []


# ---------------------------------------------------------------- prepaid card top-ups

SAVINGS_TOPUP = """Extracto de cuenta
DESDE: 2026/01/01 HASTA: 2026/03/31
CUENTA DE AHORROS
CALLE FICTICIA 1 # 2-3 NÚMERO 12345670001
SALDO ANTERIOR $ 1,000,000.00 SALDO PROMEDIO $ 950,000
TOTAL ABONOS $ .00 CUENTAS X COBRAR $ .00
TOTAL CARGOS $ 86,860.00 VALOR INTERESES PAGADOS $ .00
SALDO ACTUAL $ 913,140.00 RETEFUENTE $ .00
FECHA DESCRIPCIÓN SUCURSAL DCTO. VALOR SALDO
23/01 RECARGA DE TARJETA CIVICA -18,430.00 981,570.00
24/01 RECARGA DE TARJETA CIVICA -18,430.00 963,140.00
25/01 PAGO SUC VIRT TC MASTER DOLAR -50,000.00 913,140.00
"""


@pytest.mark.parametrize("description,hint", [
    ("RECARGA DE TARJETA CIVICA", "purchase"),
    ("Recarga Tarjeta Civica", "purchase"),
    ("PAGO SUC VIRT TC MASTER DOLAR", "card_payment"),
    ("ABONO SUCURSAL VIRTUAL TARJETA", "card_payment"),
])
def test_a_prepaid_card_top_up_is_a_purchase_not_a_credit_card_payment(description, hint):
    assert st._hint(description, outflow=True) == hint


def test_reconcile_corrects_pending_movements_it_imported_with_an_old_kind(store, tmp_path, preferences, monkeypatch):
    savings = st.parse("CTA_AHORROS", "202601", SAVINGS_TOPUP)
    report = st.reconcile([savings], [], date(2026, 1, 1))
    events = [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing]
    assert [event["kind"] for event in events] == ["purchase", "purchase", "card_payment"]
    # As the previous reader stored them: both top-ups as card payments, one already decided.
    store.record_statements(report.checks(), [
        {**event, "kind": "card_payment"} if "CIVICA" in event["merchant"] else event for event in events
    ])
    decided = events[1]["id"]
    store.decide(decided, "exclude")

    texts = {"Extracto_1_202601_CTA_AHORROS_0001.pdf": SAVINGS_TOPUP}
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=True)
    assert result["reclassified"] == 0  # read-only writes nothing
    result = run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)
    assert (result["imported"], result["reclassified"]) == (0, 1)

    kinds = {row["id"]: (row["kind"], row["decision"]) for row in store.state()["transactions"]}
    assert kinds[events[0]["id"]] == ("purchase", None)
    assert kinds[decided] == ("card_payment", "exclude")  # a person's decision stands
    # «PAGO SUC VIRT TC …» from savings is a fixed own-card payment: No cuenta.
    assert kinds[events[2]["id"]] == ("card_payment", "exclude")
    with store.connect() as db:
        audit = db.execute("SELECT target, value FROM audit WHERE action='statement-reclassify'").fetchall()
    assert [tuple(row) for row in audit] == [(events[0]["id"], "card_payment -> purchase")]
    # Repeating it changes nothing.
    assert run_reconcile(store, tmp_path, preferences, monkeypatch, texts, read_only=False)["reclassified"] == 0


def test_own_card_payment_wording_is_decided_not_counted_only_on_card_statements(store):
    def line(event_id, merchant, account, owner="santiago", kind="card_payment"):
        return statement_line(event_id, merchant, account=account, owner=owner, kind=kind, amount_minor=5000000)
    store.record_statements([], [
        line("amex", "ABONO SUCURSAL VIRTUAL", "Tarjeta de crédito Amex"),
        line("visa", "Abono  sucursal-virtual", "Tarjeta de crédito Visa", owner="manuela"),
        line("savings", "ABONO SUCURSAL VIRTUAL", "Cuenta de ahorros"),
        line("other", "ABONO SUCURSAL VIRTUAL REF 123", "Tarjeta de crédito Master"),
        line("buy", "ABONO SUCURSAL VIRTUAL", "Tarjeta de crédito Master", kind="purchase"),
    ])

    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert rows["stmt:amex"]["decision"] == rows["stmt:visa"]["decision"] == "exclude"
    assert "pago propio" in rows["stmt:amex"]["reason"]
    assert all(rows[f"stmt:{key}"]["decision"] is None for key in ("savings", "other", "buy"))
    with store.connect() as db:
        audit = db.execute("SELECT target,value FROM audit WHERE action='decision-statement-phrase'").fetchall()
    assert sorted(map(tuple, audit)) == [("stmt:amex", "exclude: ABONO SUCURSAL VIRTUAL"), ("stmt:visa", "exclude: ABONO SUCURSAL VIRTUAL")]
    assert store.monthly_totals("santiago").get("2026-01", {}).get("personal", 0) == 0

    # A person's later change stands: recording statements again does not decide it back.
    store.decide("stmt:amex", "third_party_payment")
    store.record_statements([], [])
    assert {row["id"]: row["decision"] for row in store.state()["transactions"]}["stmt:amex"] == "third_party_payment"



def test_statement_purchases_arrive_with_a_proposed_category_but_pending(store):
    store.add_rule("PEAJE", "Transporte", "family")
    store.import_message("old", [{
        "id": "old", "message_id": "old", "day": "2026-01-02", "merchant": "TIENDA LUNA",
        "account": "Tarjeta A", "amount_minor": 100, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": None, "reason": "aviso",
    }])
    store.decide("old", "personal", "Salud y farmacia")

    def line(event_id, merchant, kind="purchase"):
        return statement_line(event_id, merchant, kind=kind, amount_minor=100)

    class Down:
        def suggest_categories(self, merchants, categories, examples=None):
            raise LocalModelError("Ollama apagado")

    events = [line("a", "FLYPASS PEAJE"), line("b", "TIENDA LUNA"), line("c", "COMERCIO RARO"), line("d", "TIENDA LUNA", "transfer")]
    proposals, failed = propose_statement_categories(store, Down(), events)
    assert failed and proposals == {
        "stmt:a": {"category": "Transporte", "source": "rule"},
        "stmt:b": {"category": "Salud y farmacia", "source": "history"},
    }
    assert [event["category"] for event in events] == ["Transporte", "Salud y farmacia", "Otros gastos", "Otros gastos"]
    assert "una de tus reglas" in events[0]["reason"] and "decisiones anteriores" in events[1]["reason"]
    store.record_statements([], events)
    store.save_category_suggestions(proposals, replace=False)
    state = store.state()
    decided = {row["id"]: (row["decision"], row["category"]) for row in state["transactions"]}
    # The rule now decides its statement purchase; the rest stay pending with a proposal.
    assert decided["stmt:a"] == ("family", "Transporte") and decided["stmt:b"] == (None, "Salud y farmacia")
    # Kept for the panel, only while the movement is pending.
    assert "stmt:a" not in state["category_suggestions"]
    assert state["category_suggestions"]["stmt:b"] == {"category": "Salud y farmacia", "source": "history"}
    with pytest.raises(ValueError):
        store.save_category_suggestions({"stmt:b": {"category": "Inventada", "source": "ollama"}}, replace=True)
    assert store.state()["category_suggestions"] == {"stmt:b": {"category": "Salud y farmacia", "source": "history"}}


def test_savings_interest_and_own_card_payments_are_decided_by_their_fixed_wording(store):
    def line(event_id, merchant, account, kind):
        category = "Otros ingresos" if kind == "income" else "Otros gastos"
        return statement_line(event_id, merchant, account=account, kind=kind, category=category, owner="manuela")
    store.record_statements([], [
        line("interest", "ABONO INTERESES AHORROS", "Cuenta de ahorros", "income"),
        line("master", "PAGO SUC VIRT TC MASTER DOLAR", "Cuenta de ahorros", "card_payment"),
        line("visa", "PAGO SUC VIRT TC VISA", "Cuenta de ahorros", "card_payment"),
        line("auto", "PAGO AUTOM TC MASTER DOLAR", "Cuenta de ahorros", "card_payment"),
        line("elsewhere", "ABONO INTERESES AHORROS", "Fiducuenta", "income"),
        line("other", "PAGO SUC VIRT TC OTRA", "Cuenta de ahorros", "card_payment"),
    ])
    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert (rows["stmt:interest"]["decision"], rows["stmt:interest"]["category"]) == ("income", "Otros ingresos")
    assert rows["stmt:master"]["decision"] == rows["stmt:visa"]["decision"] == "exclude"
    assert all(rows[f"stmt:{key}"]["decision"] is None for key in ("auto", "elsewhere", "other"))
    assert "intereses" in rows["stmt:interest"]["reason"]


def test_rules_never_decide_a_statement_line_marked_as_possible_duplicate(store):
    store.record_statements([], [{
        "id": "stmt:dup", "message_id": "statement:x", "day": "2026-01-10", "merchant": "TIENDA DOBLE",
        "account": "Tarjeta de crédito Visa", "amount_minor": 1000, "currency": "COP",
        "category": "Otros gastos", "kind": "purchase", "decision": None, "owner": "santiago",
        "reason": "Leído del extracto.",
    }])
    with store.connect() as db:
        db.execute("UPDATE transactions SET note=? WHERE id='stmt:dup'", (DUPLICATE_NOTE + " de prueba",))
    store.add_rule("TIENDA DOBLE", "Compras", "personal")
    assert store.state()["transactions"][0]["decision"] is None
