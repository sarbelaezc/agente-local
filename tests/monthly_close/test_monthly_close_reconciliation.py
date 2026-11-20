"""Automatic reconciliation of a month's coverage against every owner's statements (synthetic data)."""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from monthly_close.domain import Preferences
from monthly_close.store import Store

TODAY = date(2026, 4, 15)


@pytest.fixture
def store(tmp_path, categories) -> Store:
    store = Store(tmp_path / "close.sqlite3")
    store.configure(Preferences.parse({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [], "transfer_label": "Familiar",
        "ask_savings": True, "ask_people": True, "approved_senders": ["avisos@example.test"],
        "categories": categories, "confidential_confirmed": True,
    }).to_dict())
    return store


def check(product: str, start: str, end: str, missing: int = 0, suffix: str = "0001", owner: str = "santiago") -> dict:
    return {
        "identity": f"{product}|{suffix}|{start}", "owner": owner, "product": product, "suffix": suffix,
        "period_start": start, "period_end": end, "movements": 3, "matched": 3 - missing,
        "missing": missing, "balance_minor": None,
    }


def first_quarter(**overrides) -> list[dict]:
    """A quarterly savings statement and the card statements that hold every day of Q1 2026.

    A card statement is named after the month of its mid-month cut-off, so the last
    days of March are in April's statement.
    """
    checks = {
        "savings": check("CTA_AHORROS", "2025-12-31", "2026-03-31"),
        "jan": check("TARJETA_MASTERCARD", "2026-01-01", "2026-01-31", suffix="0002"),
        "feb": check("TARJETA_MASTERCARD", "2026-02-01", "2026-02-28", suffix="0002"),
        "mar": check("TARJETA_MASTERCARD", "2026-03-01", "2026-03-31", suffix="0002"),
        "apr": check("TARJETA_MASTERCARD", "2026-04-01", "2026-04-30", suffix="0002"),
    }
    checks.update(overrides)
    return [value for value in checks.values() if value is not None]


def movement(store: Store, ident: str, day: str, owner: str = "santiago", decision: str | None = "personal"):
    store.import_message(f"m-{ident}", [{
        "id": ident, "message_id": f"m-{ident}", "day": day, "merchant": "TIENDA FICTICIA",
        "account": "Tarjeta A", "amount_minor": 1000, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": decision, "reason": "sintetico",
    }], owner=owner)


def coverage(store: Store) -> dict[str, str]:
    return {row["month"]: row["status"] for row in store.state()["coverage"]}


def test_months_fully_covered_by_matching_statements_are_reconciled(store):
    movement(store, "a", "2026-01-10")
    store.record_statements(first_quarter(), [])
    changed = store.update_reconciled_coverage(TODAY)
    assert changed == {"reconciled": ["2026-01", "2026-02", "2026-03"], "revoked": []}
    assert coverage(store) == {"2026-01": "reconciled", "2026-02": "reconciled", "2026-03": "reconciled"}
    row = store.state()["coverage"][0]
    assert row["note"].startswith("Conciliado automaticamente con 3 extractos de Santiago:")
    with store.connect() as db:
        audit = [row[0] for row in db.execute("SELECT target FROM audit WHERE action='coverage-reconciled' ORDER BY id")]
    assert audit == ["2026-01", "2026-02", "2026-03"]
    # Running it again changes nothing.
    assert store.update_reconciled_coverage(TODAY) == {"reconciled": [], "revoked": []}


def test_a_month_without_every_statement_or_with_unmatched_lines_stays_partial(store):
    for month in ("2026-01", "2026-02", "2026-03"):
        store.coverage(month, "partial", "Recorrido explicito.")
    store.record_statements(first_quarter(
        feb=None, apr=check("TARJETA_MASTERCARD", "2026-04-01", "2026-04-30", 1, "0002"),
    ), [])
    assert store.update_reconciled_coverage(TODAY)["reconciled"] == []
    # A closed month that does not qualify says why, and keeps its partial status.
    notes = {row["month"]: row["note"] for row in store.state()["coverage"]}
    assert notes["2026-01"] == "Sin conciliar con los extractos: falta el extracto 2026-02 de TARJETA_MASTERCARD *0002 de Santiago."
    assert notes["2026-02"] == "Sin conciliar con los extractos: falta el extracto 2026-02 de TARJETA_MASTERCARD *0002 de Santiago."
    assert notes["2026-03"] == (
        "Sin conciliar con los extractos: el extracto de TARJETA_MASTERCARD *0002 de Santiago tiene movimientos sin coincidir."
    )
    assert set(coverage(store).values()) == {"partial"}


def test_a_card_month_waits_for_the_next_statement_with_its_last_days(store):
    """The real case: purchases of 16 October arrived in November's card statement."""
    store.coverage("2026-01", "partial", "Recorrido explicito.")
    store.record_statements(first_quarter(feb=None, mar=None, apr=None), [])
    store.update_reconciled_coverage(TODAY)
    assert coverage(store)["2026-01"] == "partial"
    store.record_statements([check("TARJETA_MASTERCARD", "2026-02-01", "2026-02-28", suffix="0002")], [])
    store.update_reconciled_coverage(TODAY)
    assert coverage(store)["2026-01"] == "reconciled"


def test_every_owners_statements_are_required_and_named(store):
    movement(store, "a", "2026-01-10")
    for month in ("2026-01", "2026-02", "2026-03"):
        store.coverage(month, "partial", "Recorrido explicito.")
    savings = check("CTA_AHORROS", "2025-12-31", "2026-01-31", suffix="0001", owner="manuela")
    savings["identity"] = "manuela-savings-jan"
    store.record_statements(first_quarter(manuela=savings), [])
    assert store.update_reconciled_coverage(TODAY)["reconciled"] == ["2026-01"]
    rows = {row["month"]: row["note"] for row in store.state()["coverage"]}
    assert rows["2026-01"].startswith("Conciliado automaticamente con 4 extractos de Manuela y Santiago:")
    # The same product number of another owner is another product with its own statements.
    assert rows["2026-02"] == (
        "Sin conciliar con los extractos: falta el extracto de CTA_AHORROS *0001 de Manuela que cubre el 2026-02-01."
    )


def test_a_statement_keeps_the_owner_it_was_first_recorded_with(store):
    store.record_statements([check("CTA_AHORROS", "2025-12-31", "2026-03-31")], [])
    with pytest.raises(ValueError, match="otro titular"):
        store.record_statements([check("CTA_AHORROS", "2025-12-31", "2026-03-31", owner="manuela")], [])
    assert {row["owner"] for row in store.state()["statement_checks"]} == {"santiago"}


def test_a_product_that_started_later_does_not_block_earlier_months(store):
    movement(store, "a", "2026-01-10")
    store.record_statements(first_quarter(fiducuenta=check("FIDUCUENTA", "2026-03-01", "2026-03-31", suffix="0003")), [])
    store.update_reconciled_coverage(TODAY)
    assert coverage(store)["2026-01"] == "reconciled"
    assert coverage(store)["2026-03"] == "reconciled"


def test_credits_and_proteccion_never_count_as_coverage(store):
    movement(store, "a", "2026-01-10")
    store.record_statements([
        check("CREDITO", "2026-01-01", "2026-01-05"),
        check("PROTECCION_VOLUNTARIA", "2025-12-31", "2026-01-31"),
    ], [])
    assert store.update_reconciled_coverage(TODAY)["reconciled"] == []


def test_pending_decisions_of_any_owner_and_queued_notices_block_it(store):
    movement(store, "manuela-pending", "2026-01-12", owner="manuela", decision=None)
    store.record_extraction_failure("f1", "avisos@example.test", "Aviso", "2026-02-03T10:00:00-05:00", "2026-02-03", "Dinero", "merchant")
    store.record_statements(first_quarter(), [])
    store.update_reconciled_coverage(TODAY)
    assert coverage(store).get("2026-01") != "reconciled"
    assert coverage(store).get("2026-02") != "reconciled"
    assert coverage(store)["2026-03"] == "reconciled"
    store.decide("manuela-pending", "personal", "Compras")
    store.dismiss_failure("f1")
    assert store.update_reconciled_coverage(TODAY)["reconciled"] == ["2026-01", "2026-02"]


def test_a_persons_review_is_kept_and_the_current_month_is_not_judged(store):
    movement(store, "a", "2026-01-10")
    store.coverage("2026-01", "reviewed", "Comparado a mano.")
    store.record_statements(first_quarter(), [])
    store.update_reconciled_coverage(date(2026, 3, 15))
    assert coverage(store) == {"2026-01": "reviewed", "2026-02": "reconciled"}
    assert store.state()["coverage"][0]["note"] == "Comparado a mano."


def test_reconciliation_is_undone_when_it_stops_holding(store):
    movement(store, "a", "2026-01-10")
    store.record_statements(first_quarter(), [])
    store.update_reconciled_coverage(TODAY)
    store.record_statements([check("TARJETA_MASTERCARD", "2026-01-01", "2026-01-31", 2, "0002")], [])
    assert store.update_reconciled_coverage(TODAY) == {"reconciled": [], "revoked": ["2026-01"]}
    row = next(row for row in store.state()["coverage"] if row["month"] == "2026-01")
    assert row["status"] == "partial" and "sin coincidir" in row["note"]


def test_a_new_movement_reopens_a_reconciled_month(store):
    movement(store, "a", "2026-01-10")
    store.record_statements(first_quarter(), [])
    store.update_reconciled_coverage(TODAY)
    movement(store, "b", "2026-01-20", decision=None)
    assert coverage(store)["2026-01"] == "partial"


def test_a_panel_decision_reconciles_the_month_it_completes(store, start_panel):
    movement(store, "pending", "2026-01-10", decision=None)
    store.record_statements(first_quarter(), [])
    store.update_reconciled_coverage(TODAY)
    assert coverage(store).get("2026-01") != "reconciled"
    response = post(start_panel(store), "/api/decision", {"id": "pending", "decision": "personal", "category": "Compras"})
    assert response.status_code == 200, response.text
    # Real today is after January 2026, so the decision completes that month.
    assert coverage(store)["2026-01"] == "reconciled"


def changed_card(**overrides) -> list[dict]:
    """Savings for Q1 and a Mastercard whose number changed from *0002 to *0009 in March."""
    return first_quarter(**{
        "mar": check("TARJETA_MASTERCARD", "2026-03-01", "2026-03-31", suffix="0009"),
        "apr": check("TARJETA_MASTERCARD", "2026-04-01", "2026-04-30", suffix="0009"),
        **overrides,
    })


def notes(store: Store) -> dict[str, str]:
    return {row["month"]: row["note"] for row in store.state()["coverage"]}


def post(url: str, path: str, payload: dict) -> httpx.Response:
    """Send one authorised request to a running panel."""
    with httpx.Client(base_url=url, trust_env=False) as client:
        headers = {"Origin": url, "X-CSRF-Token": client.get("/api/state").json()["csrf"]}
        return client.post(path, headers=headers, json=payload)


def test_cards_seen_from_the_first_statements_are_not_asked_about(store):
    store.record_statements(first_quarter(amex=check("TARJETA_AMEX", "2026-01-01", "2026-01-31", suffix="0005")), [])
    store.update_reconciled_coverage(TODAY)
    assert store.state()["card_questions"] == []


def scanned(store: Store, *months: str) -> None:
    """Months a scan already left partial, so the reconciliation writes why they are not done."""
    for month in months:
        store.coverage(month, "partial", "Lectura del buzon")


def test_a_new_card_number_is_asked_and_joined_to_the_card_it_replaces(store, start_panel):
    movement(store, "a", "2026-01-10")
    scanned(store, "2026-02", "2026-03")
    store.record_statements(changed_card(), [])
    store.update_reconciled_coverage(TODAY)
    # Until a person answers, nothing is assumed: the old number lacks March and the new one is asked.
    assert coverage(store) == {"2026-01": "reconciled", "2026-02": "partial", "2026-03": "partial"}
    assert "falta el extracto 2026-03 de TARJETA_MASTERCARD *0002" in notes(store)["2026-02"]
    assert "falta decidir en Decisiones si la tarjeta TARJETA_MASTERCARD *0009" in notes(store)["2026-03"]
    [question] = store.state()["card_questions"]
    assert (question["owner"], question["product"], question["suffix"], question["first"]) == (
        "santiago", "TARJETA_MASTERCARD", "0009", "2026-03-01",
    )
    assert [(o["product"], o["suffix"], o["last"]) for o in question["options"]] == [
        ("TARJETA_MASTERCARD", "0002", "2026-02-01"),
    ]
    response = post(start_panel(store), "/api/card-series", {
        "owner": "santiago", "product": "TARJETA_MASTERCARD", "suffix": "0009", "replaces": "TARJETA_MASTERCARD:0002",
    })
    assert response.status_code == 200, response.text
    assert store.state()["card_questions"] == []
    assert coverage(store) == {"2026-01": "reconciled", "2026-02": "reconciled", "2026-03": "reconciled"}
    with store.connect() as db:
        audit = db.execute("SELECT target,value FROM audit WHERE action='card-series-local-user'").fetchall()
    assert [tuple(row) for row in audit] == [("santiago:TARJETA_MASTERCARD:*0009", "reemplaza TARJETA_MASTERCARD:0002")]
    # Once answered it is not asked again, and the answer is not changed from the panel.
    with pytest.raises(ValueError, match="pregunta pendiente"):
        store.decide_card_series("santiago", "TARJETA_MASTERCARD", "0009", None)


def test_a_new_credit_card_keeps_its_own_statements(store):
    store.record_statements(changed_card(
        mar=check("TARJETA_MASTERCARD", "2026-03-01", "2026-03-31", suffix="0002"),
        apr=check("TARJETA_MASTERCARD", "2026-04-01", "2026-04-30", suffix="0002"),
        new_mar=check("TARJETA_VISA", "2026-03-01", "2026-03-31", suffix="0009"),
    ), [])
    scanned(store, "2026-03")
    store.update_reconciled_coverage(TODAY)
    assert coverage(store)["2026-03"] == "partial"
    store.decide_card_series("santiago", "TARJETA_VISA", "0009", None)
    store.update_reconciled_coverage(TODAY)
    # The new card is required on its own: March still lacks its April statement.
    assert coverage(store)["2026-02"] == "reconciled"
    assert "falta el extracto 2026-04 de TARJETA_VISA *0009" in notes(store)["2026-03"]
    store.record_statements([check("TARJETA_VISA", "2026-04-01", "2026-04-30", suffix="0009")], [])
    store.update_reconciled_coverage(TODAY)
    assert coverage(store)["2026-03"] == "reconciled"


def test_a_card_can_only_be_joined_to_an_earlier_card_of_the_same_owner(store, start_panel):
    store.record_statements(changed_card(
        other=check("TARJETA_AMEX", "2026-01-01", "2026-01-31", suffix="0007", owner="manuela"),
    ), [])
    store.update_reconciled_coverage(TODAY)
    for replaces in ("TARJETA_AMEX:0007", "TARJETA_MASTERCARD:0009", "CTA_AHORROS:0001"):
        with pytest.raises(ValueError, match="tarjeta anterior"):
            store.decide_card_series("santiago", "TARJETA_MASTERCARD", "0009", replaces)
    with pytest.raises(ValueError, match="pregunta pendiente"):
        store.decide_card_series("santiago", "TARJETA_MASTERCARD", "0002", None)
    response = post(start_panel(store), "/api/card-series", {
        "owner": "santiago", "product": "TARJETA_MASTERCARD", "suffix": "0009", "replaces": 2,
    })
    assert response.status_code == 400
    assert len(store.state()["card_questions"]) == 1
