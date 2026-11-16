from __future__ import annotations

import sqlite3

import pytest

from monthly_close.store import Store

TRANSPORT = "Transporte"


def event(event_id: str, merchant: str, **updates) -> dict:
    base = {
        "id": event_id, "message_id": "m-" + event_id, "day": "2026-10-05",
        "merchant": merchant, "account": "Tarjeta A", "amount_minor": 5000000,
        "currency": "COP", "category": "Compras", "kind": "purchase",
        "decision": None, "reason": "Propuesta de Ollama.",
    }
    return base | updates


@pytest.fixture
def store(tmp_path, categories) -> Store:
    store = Store(tmp_path / "rules.sqlite3")
    store.configure({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": ["Cuenta B"],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": ["avisos@example.test"], "categories": categories,
        "confidential_confirmed": True,
    })
    return store


def rows(store: Store) -> dict[str, dict]:
    return {row["id"]: row for row in store.state()["transactions"]}


def test_rule_decides_matching_purchases_on_import_and_audits_as_rule(store, categories):
    assert TRANSPORT in categories
    store.add_rule("primax", TRANSPORT, "family")
    store.add_rule("Uber", TRANSPORT, "personal")
    store.import_message("m1", [
        event("a", "EDS PRIMAX MOBIL SUR"), event("b", "UBER*RIDES"), event("c", "TIENDA SUPERPRIMAXI"),
    ])

    result = rows(store)
    assert (result["a"]["decision"], result["a"]["category"]) == ("family", TRANSPORT)
    assert (result["b"]["decision"], result["b"]["category"]) == ("personal", TRANSPORT)
    assert "PRIMAX" in result["a"]["reason"]
    # Whole words only: a longer word containing the pattern is still asked.
    assert (result["c"]["decision"], result["c"]["category"]) == (None, "Compras")
    with sqlite3.connect(store.path) as db:
        audit = db.execute("SELECT action,value FROM audit WHERE target='a'").fetchall()
    assert audit == [("decision-rule", "family: PRIMAX")]


def test_new_rule_applies_to_pending_but_never_overrides_a_human_decision(store):
    store.import_message("m1", [event("a", "UBER RIDES"), event("b", "UBER RIDES")])
    store.decide("a", "family", "Compras")

    assert store.add_rule("UBER", TRANSPORT, "personal") == 1

    result = rows(store)
    assert (result["a"]["decision"], result["a"]["category"]) == ("family", "Compras")
    assert (result["b"]["decision"], result["b"]["category"]) == ("personal", TRANSPORT)


def test_rules_leave_non_purchases_excluded_accounts_and_usd_without_rate_pending(store):
    store.add_rule("UBER", TRANSPORT, "personal")
    store.import_message("m1", [
        event("income", "UBER", kind="income"),
        event("transfer", "UBER", kind="transfer"),
        event("excluded", "UBER", account="Cuenta B"),
        event("usd", "UBER", currency="USD"),
    ])

    assert all(row["decision"] is None for row in rows(store).values())


def test_most_specific_rule_wins_and_deleting_keeps_past_decisions(store):
    store.add_rule("IPS", "Salud y farmacia", "personal")
    store.add_rule("IPS FAMILIA", "Salud y farmacia", "family")
    store.import_message("m1", [event("a", "IPS FAMILIA SAO"), event("b", "IPS S S SAO PAULO")])
    assert [rows(store)[key]["decision"] for key in ("a", "b")] == ["family", "personal"]

    for rule in store.rules():
        store.delete_rule(rule["id"])
    store.import_message("m2", [event("c", "IPS S S SAO PAULO")])

    result = rows(store)
    assert result["a"]["decision"] == "family"
    assert result["c"]["decision"] is None


@pytest.mark.parametrize("pattern,category,decision", [
    ("ab", TRANSPORT, "personal"),
    ("***", TRANSPORT, "personal"),
    ("UBER", "Categoria inventada", "personal"),
    ("UBER", TRANSPORT, "exclude"),
    ("UBER", TRANSPORT, "investment"),
])
def test_invalid_rules_are_rejected(store, pattern, category, decision):
    with pytest.raises(ValueError):
        store.add_rule(pattern, category, decision)
    assert store.rules() == []


def test_panel_endpoints_save_list_and_delete_rules(store):
    import threading

    import httpx

    from monthly_close.server import create_server

    store.import_message("m1", [event("a", "EDS PRIMAX BOLIVARIA")])
    server = create_server(store, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            headers = {"Origin": origin, "X-CSRF-Token": client.get("/api/state").json()["csrf"]}
            saved = client.post(
                "/api/rules", headers=headers,
                json={"pattern": "primax", "category": TRANSPORT, "decision": "family"},
            )
            assert saved.status_code == 200
            state = client.get("/api/state").json()
            assert [rule["pattern"] for rule in state["rules"]] == ["PRIMAX"]
            assert state["transactions"][0]["decision"] == "family"
            bad = client.post(
                "/api/rules", headers=headers,
                json={"pattern": "primax", "category": TRANSPORT, "decision": "exclude"},
            )
            assert bad.status_code == 400
            deleted = client.post(
                "/api/rules/delete", headers=headers, json={"id": state["rules"][0]["id"]},
            )
            assert deleted.status_code == 200
            assert client.get("/api/state").json()["rules"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_income_rules_decide_only_money_received(store):
    payroll = "Nómina"
    store.add_rule("ACME", payroll, "income")
    store.import_message("m1", [
        event("pay", "PAGO NOMINA ACME SAS", kind="income", category="Otros ingresos"),
        event("buy", "TIENDA ACME"),
    ])

    result = rows(store)
    assert (result["pay"]["decision"], result["pay"]["category"]) == ("income", payroll)
    assert result["buy"]["decision"] is None
    with pytest.raises(ValueError):
        store.add_rule("OTRA", TRANSPORT, "income")
    with pytest.raises(ValueError):
        store.add_rule("OTRA", payroll, "family")


def test_dismissing_a_queued_notice_removes_it_and_blocks_normal_retries(store):
    store.record_extraction_failure(
        "notice", "avisos@example.test", "Aviso", "2026-10-05T10:00:00+00:00",
        "2026-10-05", "dinero", "amount_text",
    )
    store.dismiss_failure("notice")
    assert store.state()["extraction_failures"] == []
    assert store.seen("notice") and store.processed_without_events("notice")
    with pytest.raises(ValueError):
        store.dismiss_failure("notice")
