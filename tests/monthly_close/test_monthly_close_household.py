"""Household (Santiago + Manuela) behaviour with synthetic data only."""
from __future__ import annotations

import sqlite3

import pytest

from builders import CARD_NOTICE, SENDER, file_imported, statement_line
from monthly_close.extraction import Mail, extract
from monthly_close.store import Store


@pytest.fixture
def store(make_store, categories) -> Store:
    return make_store(categories=categories + ["Domicilios y restaurantes"])


def santiago_event(event_id: str, amount: int, **extra) -> dict:
    return {
        "id": event_id, "message_id": event_id, "day": "2026-02-10", "merchant": "TIENDA UNO",
        "account": "Tarjeta A", "amount_minor": amount, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": None, "reason": "Revisar.", **extra,
    }


def member_row(legacy: str, day: str, pesos: int, decision: str | None, **extra) -> dict:
    """One of Manuela's movements brought from her exported files."""
    return {
        "id": f"manuela:{legacy}", "message_id": f"manuela-import:{legacy}", "owner": "manuela",
        "day": day, "merchant": "COMERCIO SINTETICO", "account": "TC *0001", "amount_minor": pesos * 100,
        "currency": "COP", "category": "Compras", "kind": "purchase", "decision": decision,
        "reason": "Importado de Manuela: aprobado en su panel.", **extra,
    }


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


def test_santiago_summary_does_not_change_and_household_has_no_double_count(store):
    store.import_message("s1", [santiago_event("s1", 500000, decision="personal")])
    store.import_message("s2", [santiago_event(
        "s2", 700000, kind="transfer", merchant="Transferencia a cuenta *0008", category="Otros gastos",
    )])
    store.decide("s2", "family")
    store.import_message("s3", [santiago_event(
        "s3", 90000, kind="card_payment", category="Pago de tarjeta", decision="exclude",
    )])
    summary_before = store.summary("2026-02")

    file_imported(store, [
        member_row("m1", "2026-02-03", 2000, "personal"),
        member_row("m2", "2026-02-04", 3000, "family"),
    ])
    assert store.summary("2026-02") == summary_before
    assert "personal 2000.00; familiar 3000.00" in store.summary("2026-02", "manuela")

    assert store.set_owner_account("santiago", "0007") == 0
    assert store.set_owner_account("manuela", "0008") == 1  # Santiago's transfer to her account
    assert store.summary("2026-02") == summary_before
    # Money between members leaves the household view: only Manuela's family spending stays.
    assert "personal 7000.00; familiar 3000.00" in store.summary("2026-02", "hogar")
    assert len(audit_rows(store, "between-owners-account")) == 1


def test_only_the_newest_database_copies_are_kept(tmp_path):
    path = tmp_path / "close.sqlite3"
    copies = [f"close.sqlite3.backup-2026112{day}T120000000000Z" for day in range(5)]
    for name in copies:
        (tmp_path / name).write_bytes(b"copia")
    others = ["close.sqlite3.backup-manual", "otra.sqlite3.backup-20261120T120000000000Z"]
    for name in others:
        (tmp_path / name).write_bytes(b"no tocar")

    Store(path)

    assert sorted(p.name for p in tmp_path.glob("*.backup-*")) == sorted(copies[-3:] + others)


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


def test_paying_the_other_members_card_is_own_spending_and_never_counts_twice_at_home(store):
    """The card belongs to whom its statements say; paying one's own stays out of spending."""
    check = {
        "identity": "x", "product": "TARJETA_VISA", "suffix": "8243", "period_start": "2026-09-01",
        "period_end": "2026-09-30", "movements": 0, "matched": 0, "missing": 0, "balance_minor": None,
    }
    store.record_statements([], [])
    with store.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(statement_checks)")}
        values = {**check, "owner": "manuela", "checked_at": "2026-10-01T00:00:00+00:00"}
        values = {key: value for key, value in values.items() if key in columns}
        db.execute(
            f"INSERT INTO statement_checks ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
            list(values.values()),
        )
    assert store.other_member_cards("santiago") == {"8243"} and store.other_member_cards("manuela") == frozenset()
    mail = Mail("pay", SENDER, "Alertas", "2026-10-14T17:00:00Z", CARD_NOTICE)

    class NoModel:
        def complete_json(self, system, user):
            raise AssertionError("fixed sentence")

    own = extract(mail, store.preferences(), NoModel())
    assert [(e["kind"], e["decision"]) for e in own] == [("card_payment", "exclude")]
    events = extract(mail, store.preferences(), NoModel(), other_cards=store.other_member_cards("santiago"))
    assert [(e["kind"], e["decision"]) for e in events] == [("transfer", None)]
    store.import_message("pay", events)
    row = store.state()["transactions"][0]
    assert (row["between_owners"], row["decision"]) == (1, None)
    store.decide_group("santiago", row["group"], "personal", remember=True, category="Otros gastos")
    assert "personal 218699.00" in store.summary("2026-10")
    assert "218699" not in store.summary("2026-10", "hogar")
    # The next payment to that card follows the person's rule.
    store.import_message("pay2", [{**events[0], "id": "pay2", "message_id": "pay2"}])
    second = next(r for r in store.state()["transactions"] if r["id"] == "pay2")
    assert (second["decision"], second["category"], second["between_owners"]) == ("personal", "Otros gastos", 1)


def test_third_party_payment_only_for_card_payments_and_reduces_net(store):
    store.import_message("c1", [santiago_event("c1", 30000, kind="card_payment", category="Pago de tarjeta")])
    store.import_message("b1", [santiago_event("b1", 100000, decision="personal")])
    with pytest.raises(ValueError, match="abono"):
        store.decide("b1", "third_party_payment")
    store.decide("c1", "third_party_payment")
    assert "personal 1000.00; familiar 0.00; abonos de terceros 300.00 (restan del gasto); gasto neto 700.00" in (
        store.summary("2026-02")
    )


def test_person_confirms_an_uncertain_category_from_the_file(store):
    file_imported(store, [member_row("m1", "2026-01-03", 10000, None, category_uncertain=1)])
    store.decide("manuela:m1", "personal", "Compras")
    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert rows["manuela:m1"]["category_uncertain"] == 0


def test_destination_rule_remembers_its_category_and_decides_statement_lines(make_store):
    store = make_store("group.sqlite3", accounts=["Cuenta de ahorros"], categories=["Compras", "Otros gastos"])
    def line(event_id, day, currency="COP"):
        return statement_line(
            event_id, "TRASLADO A NEQUI", day=f"2026-01-{day}", account="Cuenta de ahorros",
            amount_minor=5000000, currency=currency, kind="unknown",
        )
    store.record_statements([], [line("a", "05"), line("b", "06")])
    store.decide_group("santiago", "com:TRASLADO A NEQUI", "personal", remember=True, category="Retiro en efectivo")

    store.record_statements([], [line("c", "20"), line("usd", "21", currency="USD")])

    rows = {row["id"]: (row["decision"], row["category"]) for row in store.state()["transactions"]}
    assert rows["stmt:c"] == ("personal", "Retiro en efectivo")
    assert rows["stmt:usd"][0] is None  # USD without a rate still waits for a person
    rule = store.state()["group_rules"][0]
    assert (rule["decision"], rule["category"]) == ("personal", "Retiro en efectivo")
