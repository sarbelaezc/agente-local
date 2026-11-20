from __future__ import annotations

import sqlite3

import httpx
import pytest

from builders import movement
from monthly_close.rule_suggestions import suggest_pending_categories, suggest_rules
from monthly_close.store import Store

TRANSPORT = "Transporte"


@pytest.fixture
def store(make_store) -> Store:
    return make_store("rules.sqlite3", excluded_accounts=["Cuenta B"])


def rows(store: Store) -> dict[str, dict]:
    return {row["id"]: row for row in store.state()["transactions"]}


def test_rule_decides_matching_purchases_on_import_and_audits_as_rule(store, categories):
    assert TRANSPORT in categories
    store.add_rule("primax", TRANSPORT, "family")
    store.add_rule("Uber", TRANSPORT, "personal")
    store.import_message("m1", [
        movement("a", "EDS PRIMAX MOBIL SUR"), movement("b", "UBER*RIDES"), movement("c", "TIENDA SUPERPRIMAXI"),
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
    store.import_message("m1", [movement("a", "UBER RIDES"), movement("b", "UBER RIDES")])
    store.decide("a", "family", "Compras")

    assert store.add_rule("UBER", TRANSPORT, "personal") == 1

    result = rows(store)
    assert (result["a"]["decision"], result["a"]["category"]) == ("family", "Compras")
    assert (result["b"]["decision"], result["b"]["category"]) == ("personal", TRANSPORT)


def test_rules_leave_non_purchases_excluded_accounts_and_usd_without_rate_pending(store):
    store.add_rule("UBER", TRANSPORT, "personal")
    store.import_message("m1", [
        movement("income", "UBER", kind="income"),
        movement("transfer", "UBER", kind="transfer"),
        movement("excluded", "UBER", account="Cuenta B"),
        movement("usd", "UBER", currency="USD"),
    ])

    assert all(row["decision"] is None for row in rows(store).values())


def test_most_specific_rule_wins_and_deleting_keeps_past_decisions(store):
    store.add_rule("IPS", "Salud y farmacia", "personal")
    store.add_rule("IPS FAMILIA", "Salud y farmacia", "family")
    store.import_message("m1", [movement("a", "IPS FAMILIA SAO"), movement("b", "IPS S S SAO PAULO")])
    assert [rows(store)[key]["decision"] for key in ("a", "b")] == ["family", "personal"]

    for rule in store.rules():
        store.delete_rule(rule["id"])
    store.import_message("m2", [movement("c", "IPS S S SAO PAULO")])

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


def test_panel_endpoints_save_list_and_delete_rules(store, start_panel):
    store.import_message("m1", [movement("a", "EDS PRIMAX BOLIVARIA")])
    origin = start_panel(store)
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


def test_income_rules_decide_only_money_received(store):
    payroll = "Nómina"
    store.add_rule("ACME", payroll, "income")
    store.import_message("m1", [
        movement("pay", "PAGO NOMINA ACME SAS", kind="income", category="Otros ingresos"),
        movement("buy", "TIENDA ACME"),
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


class FakeCategoryModel:
    def __init__(self, suggestions):
        self.suggestions = suggestions
        self.asked = None

    def suggest_categories(self, merchants, categories, examples=None):
        self.asked = (merchants, categories)
        self.examples = examples
        return {"suggestions": self.suggestions}


def test_suggestions_cover_only_unruled_merchants_and_save_nothing(store):
    store.add_rule("UBER", TRANSPORT, "personal")
    store.import_message("m1", [
        movement("a", "UBER*RIDES"), movement("b", "FLYPASS PEAJE"), movement("c", "FLYPASS PEAJE"),
        movement("d", "TIENDA LUNA", kind="transfer"),
    ])
    model = FakeCategoryModel([
        {"merchant": "FLYPASS PEAJE", "category": TRANSPORT},
        {"merchant": "UBER RIDES", "category": TRANSPORT},  # already covered by a rule
        {"merchant": "OTRO", "category": TRANSPORT},  # never asked
        {"merchant": "FLYPASS PEAJE", "category": "Inventada"},  # unauthorized category
        "basura",
    ])

    assert suggest_rules(store, model) == [{"pattern": "FLYPASS PEAJE", "category": TRANSPORT, "count": 2}]
    assert model.asked[0] == ["FLYPASS PEAJE"]
    assert len(store.rules()) == 1
    assert rows(store)["b"]["decision"] is None


def test_suggestion_with_no_pending_merchants_does_not_call_the_model(store):
    class Unused:
        def suggest_categories(self, merchants, categories, examples=None):
            raise AssertionError("no merchants to ask about")

    assert suggest_rules(store, Unused()) == []


def test_suggest_endpoint_needs_csrf_and_a_model_and_never_writes(store, start_panel):
    store.import_message("m1", [movement("a", "FLYPASS PEAJE")])
    model = FakeCategoryModel([{"merchant": "FLYPASS PEAJE", "category": TRANSPORT}])
    results = []
    for url in (start_panel(store, model), start_panel(store)):
        with httpx.Client(base_url=url, trust_env=False) as client:
            headers = {"Origin": url, "X-CSRF-Token": client.get("/api/state").json()["csrf"]}
            assert client.post("/api/rules/suggest", json={}).status_code == 403
            results.append(client.post("/api/rules/suggest", headers=headers, json={}))

    assert results[0].status_code == 200
    assert results[0].json() == {"suggestions": [{"pattern": "FLYPASS PEAJE", "category": TRANSPORT, "count": 1}]}
    assert results[1].status_code == 400 and "Ollama" in results[1].json()["error"]
    assert store.rules() == [] and rows(store)["a"]["decision"] is None


def test_pending_category_suggestions_include_statement_purchases_and_save_nothing(store):
    store.import_message("m1", [
        movement("stmt:1", "FLYPASS PEAJE"), movement("stmt:2", "FLYPASS PEAJE"),
        movement("a", "TIENDA LUNA"), movement("i", "EMPRESA SA", kind="income"),
    ])
    model = FakeCategoryModel([
        {"merchant": "FLYPASS PEAJE", "category": TRANSPORT},
        {"merchant": "TIENDA LUNA", "category": "Inventada"},
        {"merchant": "EMPRESA SA", "category": TRANSPORT},
    ])

    suggestion = {"category": TRANSPORT, "source": "ollama"}
    assert suggest_pending_categories(store, model) == {"stmt:1": suggestion, "stmt:2": suggestion}
    assert set(model.asked[0]) == {"FLYPASS PEAJE", "TIENDA LUNA"}
    assert all(row["decision"] is None and row["category"] != TRANSPORT for row in rows(store).values())


def test_group_decision_applies_the_chosen_category_all_or_nothing(store):
    store.import_message("m1", [movement("stmt:1", "FLYPASS PEAJE"), movement("stmt:2", "FLYPASS PEAJE")])
    group = "com:FLYPASS PEAJE"
    owner = rows(store)["stmt:1"]["owner"]

    with pytest.raises(ValueError):
        store.decide_group(owner, group, "exclude", category=TRANSPORT)
    with pytest.raises(ValueError):
        store.decide_group(owner, group, "family", category="Inventada")
    assert all(row["decision"] is None for row in rows(store).values())

    assert store.decide_group(owner, group, "family", category=TRANSPORT) == 2
    assert {(row["decision"], row["category"]) for row in rows(store).values()} == {("family", TRANSPORT)}


def test_rules_and_consistent_past_decisions_answer_without_the_model(store):
    store.add_rule("PEAJE", TRANSPORT, "family")
    store.import_message("old", [
        movement("h1", "TIENDA LUNA 123"), movement("h2", "TIENDA LUNA 123"),
        movement("x1", "DROGUERIA SOL"), movement("x2", "DROGUERIA SOL"),
    ])
    store.decide("h1", "personal", "Salud y farmacia")
    store.decide("h2", "family", "Salud y farmacia")
    store.decide("x1", "personal", "Salud y farmacia")
    store.decide("x2", "personal", "Compras")  # ambiguous merchant: only an example
    store.import_message("stmt", [
        movement("stmt:1", "FLYPASS PEAJE"), movement("stmt:2", "TIENDA LUNA 123"),
        movement("stmt:3", "DROGUERIA SOL"), movement("stmt:4", "LUNA TIENDA NORTE"),
    ])
    model = FakeCategoryModel([{"merchant": "DROGUERIA SOL", "category": "Compras"}])

    result = suggest_pending_categories(store, model)

    # The rule decided its statement purchase on import, so it needs no suggestion.
    assert (rows(store)["stmt:1"]["decision"], rows(store)["stmt:1"]["category"]) == ("family", TRANSPORT)
    assert "stmt:1" not in result
    assert result["stmt:2"] == {"category": "Salud y farmacia", "source": "history"}
    assert result["stmt:3"] == {"category": "Compras", "source": "ollama"}
    assert sorted(model.asked[0]) == ["DROGUERIA SOL", "LUNA TIENDA NORTE"]
    # Examples are the confirmed merchants that share words with the asked ones.
    assert {"merchant": "TIENDA LUNA 123", "category": "Salud y farmacia"} in model.examples
    assert {"merchant": "DROGUERIA SOL", "category": "Compras"} in model.examples
    assert all(row["decision"] is None for key, row in rows(store).items() if key in {"stmt:2", "stmt:3", "stmt:4"})
