"""A credit's payment register as complementary evidence, with a fictitious workbook."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from openpyxl import Workbook

from monthly_close import credit_ledger
from monthly_close import main as app
from monthly_close import statements as st
from monthly_close.domain import Preferences
from monthly_close.store import Store

HEADER = ["", "Cuota", "Fecha", "Pago", "Abono Capital", "Pago Capital", "Pago Intereses",
          "Seguros y Cargos Adicionales", "Pago Total"]
# Between the January and February cut-offs the capital drops by 1,000,000.
ROWS = [
    ["", "?", "???", 5000, 5000, 0, 0, 0, 5000],
    ["", 0, datetime(2025, 1, 15), 503047, 0, 294548, 203553, 4946, 10376],  # older typo: ignored
    ["", 1, datetime(2026, 1, 15), 355000, 0, 300000, 50000, 5000, 355000],
    ["", 1, datetime(2026, 1, 20), 500000, 500000, 0, 0, 0, 500000],
    ["", 2, datetime(2026, 1, 30), 245000, 0, 200000, 40000, 5000, 245000],
    ["", 3, datetime(2026, 2, 15), 355000, 0, 0, 0, 0, 0],  # still to be paid
]


def credit_text(cut: str, balance: str, paid: str) -> str:
    return (
        "OBLIGACIÓN Nº: 5550004444\n"
        f"TOTAL {paid} 600,000.00\n"
        f"FECHA CORTE EXTRACTO {cut}\n"
        f"SALDO DE CAPITAL {balance}\n"
    )


def statement_instalments(store: Store) -> int:
    with store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM transactions WHERE message_id LIKE 'statement:CREDITO:%'").fetchone()[0]


@pytest.fixture
def setup(tmp_path, categories):
    def build(rows=ROWS, bonus_months=(6, 12)):
        folder = tmp_path / f"case{len(list(tmp_path.iterdir()))}"
        folder.mkdir()
        book = Workbook()
        sheet = book.active
        sheet.title = "Hoja1"
        sheet.append(HEADER)
        for row in rows:
            sheet.append(row)
        book.save(folder / "pagos.xlsx")
        (folder / credit_ledger.LEDGERS_FILE).write_text(json.dumps([{
            "owner": "santiago", "suffix": "4444", "name": "Carro de prueba", "path": str(folder / "pagos.xlsx"),
            "sheet": "Hoja1", "bonus_months": list(bonus_months), "category": "Transporte",
        }]), encoding="utf-8")
        store = Store(folder / "close.sqlite3")
        preferences = Preferences.parse({
            "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
            "accounts": ["Tarjeta A"], "excluded_accounts": [], "transfer_label": "Familiar",
            "ask_savings": True, "ask_people": True, "approved_senders": ["avisos@example.test"],
            "categories": categories, "confidential_confirmed": True,
        })
        store.configure(preferences.to_dict())
        statements = [
            st.parse("CREDITO", "202601", credit_text("1/05/2026", "9,000,000.00", "245,000.00")),
            st.parse("CREDITO", "202602", credit_text("2/05/2026", "8,000,000.00", "245,000.00")),
        ]
        report = st.reconcile(statements, [], date(2026, 1, 1))
        store.record_statements(report.checks(), [st.missing_event(c, "Otros gastos", "Otros ingresos") for c in report.missing])
        return folder, store, preferences
    return build


def test_register_is_read_only_when_it_matches_the_bank(setup):
    folder, store, preferences = setup()
    store.import_message("own", [{
        "id": "own", "message_id": "own", "day": "2026-01-20", "merchant": "PAGO DE CREDITO", "account": "Cuenta de ahorros",
        "amount_minor": 50000000, "currency": "COP", "category": "Otros gastos", "kind": "loan", "decision": None,
        "reason": "Revisar.",
    }])
    lines = app.import_credit_ledgers(store, folder, preferences)
    assert "3 pagos leidos" in lines[0] and "1 posibles duplicados" in lines[0]
    rows = {r["id"]: r for r in store.state()["transactions"]}
    ledger = sorted((r for r in rows.values() if r["id"].startswith("ledger:")), key=lambda r: r["day"])
    assert [(r["day"], r["amount_minor"], r["decision"], r["category"]) for r in ledger] == [
        ("2026-01-15", 35500000, None, "Transporte"), ("2026-01-20", 50000000, None, "Transporte"),
        ("2026-01-30", 24500000, None, "Transporte"),
    ]
    assert ledger[0]["note"] == "Capital 300.000 · intereses 50.000 · seguros 5.000"
    assert statement_instalments(store) == 0
    assert "registro de pagos" in rows["own"]["note"] and rows["own"]["decision"] is None
    again = app.import_credit_ledgers(store, folder, preferences)
    assert "0 nuevos" in again[0]


def test_register_that_does_not_match_the_bank_imports_nothing(setup):
    rows = [list(row) for row in ROWS]
    rows[3][4] = rows[3][3] = rows[3][8] = 400000  # a capital payment the bank never saw
    folder, store, preferences = setup(rows)
    with pytest.raises(ValueError, match="no cuadra"):
        app.import_credit_ledgers(store, folder, preferences)
    assert not any(r["id"].startswith("ledger:") for r in store.state()["transactions"])


def test_payroll_income_only_for_fortnights_and_pending_for_bonus_extras(setup):
    for bonus_months, extra_twin in [((6, 12), False), ((1,), True)]:
        folder, store, preferences = setup(bonus_months=bonus_months)
        app.import_credit_ledgers(store, folder, preferences)
        store.set_payroll_deduction("santiago", "4444")
        for row in store.state()["transactions"]:
            if row["id"].startswith("ledger:"):
                store.decide(row["id"], "family")
        rows = store.state()["transactions"]
        fortnights = [r for r in rows if r["id"].startswith("payroll:")]
        assert sorted(r["amount_minor"] for r in fortnights) == [24500000, 35500000]
        assert {r["decision"] for r in fortnights} == {"income"}
        extras = [r for r in rows if r["id"].startswith("payroll-extra:")]
        assert [(r["amount_minor"], r["decision"]) for r in extras] == ([(50000000, None)] if extra_twin else [])


def test_statement_instalments_are_not_imported_again_for_a_credit_with_register(setup, monkeypatch):
    folder, store, preferences = setup()
    app.import_credit_ledgers(store, folder, preferences)
    (folder / "statements.json").write_text(json.dumps({"password": "x", "approved_senders": ["banco@example.test"]}), encoding="utf-8")
    texts = {"Extracto_1_202602_CREDITO_4444.pdf": credit_text("2/05/2026", "8,000,000.00", "245,000.00")}

    class Reader:
        def statement_files(self, *args):
            for name in texts:
                yield name, name.encode()

    from contextlib import contextmanager

    @contextmanager
    def fake_source(*args):
        yield Reader()

    monkeypatch.setattr(app, "open_source", fake_source)
    monkeypatch.setattr(st, "decrypt_text", lambda content, password: texts[content.decode()])
    result = app.reconcile_statements(store, folder, {}, preferences, date(2026, 1, 1), False)
    assert result["imported"] == 0
    assert statement_instalments(store) == 0


def test_register_settings_are_validated(tmp_path):
    assert credit_ledger.load_configs(tmp_path) == []
    (tmp_path / credit_ledger.LEDGERS_FILE).write_text(json.dumps([{"suffix": "12a4"}]), encoding="utf-8")
    with pytest.raises(credit_ledger.LedgerError):
        credit_ledger.load_configs(tmp_path)
