"""Household (Santiago + Manuela) behaviour with synthetic data only."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from monthly_close.household_import import (
    DECISIONS_FILE, MOVEMENTS_FILE, PARITY_FILE, import_member, parity_report,
)
from monthly_close.store import Store

SRC = Path(__file__).parents[2] / "src"


@pytest.fixture
def store(tmp_path, categories) -> Store:
    store = Store(tmp_path / "close.sqlite3")
    store.configure({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": ["avisos@example.test"],
        "categories": categories + ["Domicilios y restaurantes"],
        "confidential_confirmed": True,
    })
    return store


def santiago_event(event_id: str, amount: int, **extra) -> dict:
    return {
        "id": event_id, "message_id": event_id, "day": "2026-02-10", "merchant": "TIENDA UNO",
        "account": "Tarjeta A", "amount_minor": amount, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": None, "reason": "Revisar.", **extra,
    }


def member_record(legacy: str, day: str, pesos: int, kind: str, proposal: str, **extra) -> dict:
    return {
        "legacy_id": legacy, "owner": "manuela", "day": day, "time": None,
        "merchant": extra.pop("merchant", "COMERCIO SINTETICO"), "account": "TC *0001",
        "account_label": "Tarjeta sintetica *0001", "amount_minor": pesos * 100,
        "currency": "COP", "category": extra.pop("category", "Compras"),
        "category_uncertain": extra.pop("category_uncertain", False), "kind": kind,
        "decision_proposal": proposal, "group_key": extra.pop("group_key", None),
        "note": extra.pop("note", None), "legacy_decision": None, "legacy_state": "x",
        "legacy_type": "x", **extra,
    }


SYNTHETIC = [
    member_record("m1", "2026-01-03", 10000, "purchase", "personal", category_uncertain=True),
    member_record("m2", "2026-01-04", 4000, "purchase", "personal", note="USD 1 a 4000 COP (aprox.)"),
    member_record("m3", "2026-01-05", 50000, "income", "income", category=None, income_category="Nómina"),
    member_record("m4", "2026-01-06", 3000, "card_payment", "exclude", category="Pagos de otras personas"),
    member_record("m5", "2026-01-07", 7000, "transfer", "family", category=None, group_key="tr:99990001234"),
    member_record("m6", "2026-02-07", 2000, "transfer", "family", category=None, group_key="tr:99990001234"),
    member_record("m7", "2026-02-08", 1500, "unknown", "cash_withdrawal", category=None, group_key="cj:cajero"),
    member_record("m8", "2026-02-09", 9000, "unknown", "investment_withdrawal", category=None, group_key="fd:f001"),
    member_record("m9", "2026-02-10", 800, "transfer", "exclude", category=None, group_key="tr:88880005678"),
]
DESTINATIONS = [
    {"group_key": "tr:99990001234", "decision": "familiar"},
    {"group_key": "cj:cajero", "decision": "cajero"},
    {"group_key": "fd:f001", "decision": "inversion"},
    {"group_key": "tr:88880005678", "decision": "propia"},
]
# Manuela-style totals: cash withdrawals and fund withdrawals did not count there.
PARITY = {
    "2026-01": {"gasto_personal": 14000, "gasto_familiar": 7000, "ingresos": 50000,
                "terceros": 3000, "gasto_neto_dashboard": 18000},
    "2026-02": {"gasto_personal": 0, "gasto_familiar": 2000, "ingresos": 0,
                "terceros": 0, "gasto_neto_dashboard": 2000},
}


def write_member_files(folder: Path, records=SYNTHETIC, destinations=DESTINATIONS, parity=PARITY) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / MOVEMENTS_FILE).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    (folder / DECISIONS_FILE).write_text(json.dumps(destinations), encoding="utf-8")
    (folder / PARITY_FILE).write_text(json.dumps(parity), encoding="utf-8")
    return folder


def audit_rows(store: Store, action: str | None = None) -> list[sqlite3.Row]:
    with store.connect() as db:
        if action is None:
            return db.execute("SELECT * FROM audit ORDER BY id").fetchall()
        return db.execute("SELECT * FROM audit WHERE action=? ORDER BY id", (action,)).fetchall()


def test_migration_backs_up_and_keeps_existing_rows_as_santiago(tmp_path):
    path = tmp_path / "close.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE transactions (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, day TEXT NOT NULL,
            merchant TEXT NOT NULL, account TEXT NOT NULL, amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL,
            decision TEXT, reason TEXT NOT NULL
        );
        CREATE TABLE audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL, value TEXT NOT NULL);
        INSERT INTO transactions VALUES ('a','m','2026-01-02','TIENDA','Tarjeta A',12345,'COP',
            'Compras','purchase','personal','Revisar.');
        INSERT INTO audit(at,action,target,value) VALUES ('t','decision-local-user','a','personal');
    """)
    old.commit()
    old.close()

    store = Store(path)

    backups = list(tmp_path.glob("close.sqlite3.backup-*"))
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    assert copy.execute("SELECT id,amount_minor,decision FROM transactions").fetchall() == [
        ("a", 12345, "personal"),
    ]
    assert "owner" not in {row[1] for row in copy.execute("PRAGMA table_info(transactions)")}
    copy.close()
    row = store.state()["transactions"][0]
    assert (row["owner"], row["amount_minor"], row["decision"], row["group_key"]) == (
        "santiago", 12345, "personal", None,
    )
    assert row["between_owners"] == 0 and row["category_uncertain"] == 0
    actions = [entry["action"] for entry in audit_rows(store)]
    assert actions == ["decision-local-user", "migrate-household"]
    Store(path)  # already migrated: no second copy
    assert len(list(tmp_path.glob("close.sqlite3.backup-*"))) == 1


def test_member_import_is_validated_audited_and_idempotent(store, tmp_path):
    folder = write_member_files(tmp_path / "import")

    first = import_member(store, folder)
    assert first == {
        "imported": 9, "skipped": 0, "approved": 4, "by_destination": 5, "pending": 0,
        "between_owners": 0,
    }
    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert rows["manuela:m4"]["decision"] == "third_party_payment"
    assert rows["manuela:m3"]["category"] == "Nómina"
    assert rows["manuela:m7"]["category"] == "Retiro en efectivo"
    assert rows["manuela:m8"]["decision"] == "investment_withdrawal"
    assert rows["manuela:m9"]["decision"] == "exclude"
    assert rows["manuela:m1"]["category_uncertain"] == 1
    assert rows["manuela:m2"]["note"].startswith("USD 1")
    assert all(row["owner"] == "manuela" for row in rows.values())
    assert len(audit_rows(store, "decision-manuela")) == 4
    assert len(audit_rows(store, "decision-group-rule")) == 5
    assert len(audit_rows(store, "group-rule-manuela")) == 4
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM processed").fetchone()[0] == 0

    before = audit_rows(store)
    second = import_member(store, folder)
    assert second["imported"] == 0 and second["skipped"] == 9
    after = audit_rows(store)
    assert [entry["action"] for entry in after[len(before):]] == ["import-manuela"]
    assert {row["id"]: row for row in store.state()["transactions"]} == rows


def test_invalid_member_file_imports_nothing(store, tmp_path):
    broken = [*SYNTHETIC[:3], {**SYNTHETIC[3], "amount_minor": -5}]
    folder = write_member_files(tmp_path / "import", records=broken)
    with pytest.raises(ValueError, match="Movimiento 4"):
        import_member(store, folder)
    assert store.state()["transactions"] == []

    contradictory = [*DESTINATIONS, {"group_key": "cj:cajero", "decision": "personal"}]
    folder = write_member_files(tmp_path / "import2", destinations=contradictory)
    with pytest.raises(ValueError, match="contradictorias"):
        import_member(store, folder)
    assert store.state()["transactions"] == []


def test_member_parity_matches_except_approved_cash_withdrawals(store, tmp_path):
    folder = write_member_files(tmp_path / "import")
    import_member(store, folder)

    matched, lines = parity_report(store, folder / PARITY_FILE)
    assert matched, lines
    assert lines[0] == "2026-01: coincide."
    assert lines[1] == "2026-02: coincide (+1.500 COP de retiro en cajero, aprobado)."
    totals = store.monthly_totals("manuela")
    assert totals["2026-02"]["personal"] == 150000  # the cash withdrawal counts here
    assert totals["2026-01"]["net"] == (14000 + 7000 - 3000) * 100

    wrong = {**PARITY, "2026-01": {**PARITY["2026-01"], "ingresos": 1}}
    (folder / PARITY_FILE).write_text(json.dumps(wrong), encoding="utf-8")
    matched, lines = parity_report(store, folder / PARITY_FILE)
    assert not matched and "ingresos" in lines[0]


def test_santiago_totals_do_not_change_and_household_has_no_double_count(store, tmp_path):
    store.import_message("s1", [santiago_event("s1", 500000, decision="personal")])
    store.import_message("s2", [santiago_event(
        "s2", 700000, kind="transfer", merchant="Transferencia a cuenta *0008", category="Otros gastos",
    )])
    store.decide("s2", "family")
    store.import_message("s3", [santiago_event(
        "s3", 90000, kind="card_payment", category="Pago de tarjeta", decision="exclude",
    )])
    summary_before = store.summary("2026-02")
    totals_before = store.monthly_totals("santiago")

    import_member(store, write_member_files(tmp_path / "import"))
    assert store.summary("2026-02") == summary_before
    assert store.monthly_totals("santiago") == totals_before

    assert store.set_owner_account("santiago", "0007") == 0
    assert store.set_owner_account("manuela", "0008") == 1  # Santiago's transfer to her account
    assert store.monthly_totals("santiago") == totals_before
    household = store.monthly_totals("hogar")
    manuela = store.monthly_totals("manuela")
    for month in ("2026-01", "2026-02"):
        for field in ("personal", "family", "income", "third_party", "net"):
            expected = totals_before.get(month, {}).get(field, 0) + manuela[month][field]
            if month == "2026-02" and field in {"family", "net"}:
                expected -= 700000  # money between members leaves the household view
            assert household[month][field] == expected, (month, field)
    assert "familiar 7000.00" in store.summary("2026-02")
    assert "familiar 2000.00" in store.summary("2026-02", "hogar")  # only Manuela's
    assert len(audit_rows(store, "between-owners-account")) == 1


def test_destination_decision_is_atomic_audited_and_can_be_remembered(store):
    for event_id in ("p1", "p2"):
        store.import_message(event_id, [santiago_event(event_id, 1000, merchant="Café Central")])
    store.import_message("usd", [santiago_event("usd", 500, merchant="CAFE CENTRAL", currency="USD")])
    group = "com:CAFÉ CENTRAL"
    assert {row["group"] for row in store.state()["transactions"]} == {group, "com:CAFE CENTRAL"}

    assert store.decide_group("santiago", group, "personal", remember=True) == 2
    assert [entry["target"] for entry in audit_rows(store, "decision-group-local-user")] == ["p1", "p2"]
    assert audit_rows(store, "group-rule-local-user")[0]["value"] == "personal"

    store.import_message("p3", [santiago_event("p3", 1000, merchant="CAFÉ  central")])
    decided = {row["id"]: row["decision"] for row in store.state()["transactions"]}
    assert decided["p3"] == "personal"
    assert audit_rows(store, "decision-group-rule")[0]["target"] == "p3"

    # One movement fails validation (USD without a rate): nothing in the group changes.
    with pytest.raises(ValueError, match="tasa"):
        store.decide_group("santiago", "com:CAFE CENTRAL", "personal")
    assert {row["id"]: row["decision"] for row in store.state()["transactions"]}["usd"] is None
    with pytest.raises(ValueError, match="Titular"):
        store.decide_group("hogar", group, "personal")

    store.delete_group_rule("santiago", group)
    store.import_message("p4", [santiago_event("p4", 1000, merchant="Café Central")])
    assert {row["id"]: row["decision"] for row in store.state()["transactions"]}["p4"] is None


def test_between_owners_mark_is_manual_audited_and_remembered(store):
    store.import_message("t1", [santiago_event("t1", 1000, kind="transfer", merchant="MARIA")])
    group = "com:MARIA"
    assert store.mark_between_owners("santiago", group, True, remember=True) == 1
    store.import_message("t2", [santiago_event("t2", 2000, kind="transfer", merchant="Maria")])
    marks = {row["id"]: row["between_owners"] for row in store.state()["transactions"]}
    assert marks == {"t1": 1, "t2": 1}
    assert len(audit_rows(store, "between-owners-local-user")) == 1
    assert len(audit_rows(store, "between-owners-group-rule")) == 1
    store.mark_between_owners("santiago", group, False, remember=True)
    assert store.state()["group_rules"] == []
    with pytest.raises(ValueError):
        store.set_owner_account("santiago", "12345")


def test_third_party_payment_only_for_card_payments_and_reduces_net(store):
    store.import_message("c1", [santiago_event("c1", 30000, kind="card_payment", category="Pago de tarjeta")])
    store.import_message("b1", [santiago_event("b1", 100000, decision="personal")])
    with pytest.raises(ValueError, match="abono"):
        store.decide("b1", "third_party_payment")
    store.decide("c1", "third_party_payment")
    totals = store.monthly_totals("santiago")["2026-02"]
    assert (totals["personal"], totals["third_party"], totals["net"]) == (100000, 30000, 70000)
    assert "gasto neto 700.00" in store.summary("2026-02")


def test_person_confirms_uncertain_category_but_import_does_not(store, tmp_path):
    import_member(store, write_member_files(tmp_path / "import"))
    store.decide("manuela:m1", "personal", "Compras")
    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert rows["manuela:m1"]["category_uncertain"] == 0


def test_cli_imports_member_files_without_mailbox_or_ollama(tmp_path, store):
    folder = write_member_files(tmp_path / "import")
    data = tmp_path / "data"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    unconfigured = subprocess.run(
        [sys.executable, "-m", "monthly_close.main", "import-manuela",
         "--data-dir", str(data), "--source-dir", str(folder)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert unconfigured.returncode == 1 and "paso 0" in unconfigured.stderr
    assert Store(data / "close.sqlite3").state()["transactions"] == []
    data = store.path.parent  # the configured synthetic store
    result = subprocess.run(
        [sys.executable, "-m", "monthly_close.main", "import-manuela",
         "--data-dir", str(data), "--source-dir", str(folder)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "9 nuevos" in result.stdout and "2026-02: coincide" in result.stdout
    result = subprocess.run(
        [sys.executable, "-m", "monthly_close.main", "summary", "--month", "2026-01",
         "--owner", "manuela", "--data-dir", str(data)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "abonos de terceros 3000.00" in result.stdout


def test_destination_rule_remembers_its_category_and_decides_statement_lines(tmp_path):
    from monthly_close.store import Store

    store = Store(tmp_path / "group.sqlite3")
    store.configure({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Cuenta de ahorros"], "excluded_accounts": [], "transfer_label": "Familiar",
        "ask_savings": True, "ask_people": True, "approved_senders": ["avisos@example.test"],
        "categories": ["Compras", "Otros gastos"], "confidential_confirmed": True,
    })
    def line(event_id, day, currency="COP"):
        return {
            "id": f"stmt:{event_id}", "message_id": "statement:x", "day": f"2026-01-{day}",
            "merchant": "TRASLADO A NEQUI", "account": "Cuenta de ahorros", "amount_minor": 5000000,
            "currency": currency, "category": "Otros gastos", "kind": "unknown", "decision": None,
            "owner": "santiago", "reason": "Leído del extracto.",
        }
    store.record_statements([], [line("a", "05"), line("b", "06")])
    store.decide_group("santiago", "com:TRASLADO A NEQUI", "personal", remember=True, category="Retiro en efectivo")

    store.record_statements([], [line("c", "20"), line("usd", "21", currency="USD")])

    rows = {row["id"]: (row["decision"], row["category"]) for row in store.state()["transactions"]}
    assert rows["stmt:c"] == ("personal", "Retiro en efectivo")
    assert rows["stmt:usd"][0] is None  # USD without a rate still waits for a person
    rule = store.state()["group_rules"][0]
    assert (rule["decision"], rule["category"]) == ("personal", "Retiro en efectivo")
