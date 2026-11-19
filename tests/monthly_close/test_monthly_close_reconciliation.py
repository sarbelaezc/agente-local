"""Automatic reconciliation of a month's coverage against Santiago's statements (synthetic data)."""
from __future__ import annotations

import threading
from datetime import date

import httpx
import pytest

from monthly_close.domain import Preferences
from monthly_close.server import create_server
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


def check(product: str, start: str, end: str, missing: int = 0, suffix: str = "0001") -> dict:
    return {
        "identity": f"{product}|{suffix}|{start}", "product": product, "suffix": suffix,
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
    assert "3 extractos de Santiago" in row["note"] and "Manuela no tienen extractos" in row["note"]
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
    assert notes["2026-01"] == "Sin conciliar con los extractos: falta el extracto de TARJETA_MASTERCARD de 2026-02."
    assert notes["2026-02"] == "Sin conciliar con los extractos: falta el extracto de TARJETA_MASTERCARD de 2026-02."
    assert notes["2026-03"] == "Sin conciliar con los extractos: el extracto de TARJETA_MASTERCARD tiene movimientos sin coincidir."
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


def test_a_panel_decision_reconciles_the_month_it_completes(store):
    movement(store, "pending", "2026-01-10", decision=None)
    store.record_statements(first_quarter(), [])
    store.update_reconciled_coverage(TODAY)
    assert coverage(store).get("2026-01") != "reconciled"
    server = create_server(store, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            headers = {"Origin": url, "X-CSRF-Token": client.get("/api/state").json()["csrf"]}
            response = client.post("/api/decision", headers=headers, json={"id": "pending", "decision": "personal", "category": "Compras"})
            assert response.status_code == 200, response.text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    # Real today is after January 2026, so the decision completes that month.
    assert coverage(store)["2026-01"] == "reconciled"
