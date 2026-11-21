"""Dollar purchases of one destination: one rate for all of them, and rules that wait for it."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect


def purchase(event_id: str, amount: int, *, currency: str = "USD", merchant: str = "STREAMING SINTETICO") -> dict:
    return {
        "id": event_id, "message_id": event_id, "day": "2026-03-10", "merchant": merchant,
        "account": "Tarjeta A", "amount_minor": amount, "currency": currency, "category": "Compras",
        "kind": "purchase", "decision": None, "reason": "Revisar.",
    }


def rows(store) -> dict[str, dict]:
    return {tx["id"]: tx for tx in store.state()["transactions"]}


def test_one_rate_decides_every_dollar_movement_of_a_destination(make_store):
    store = make_store()
    store.import_message("u1", [purchase("u1", 1000)])
    store.import_message("u2", [purchase("u2", 2550)])
    store.import_message("c1", [purchase("c1", 40000, currency="COP")])
    group = rows(store)["u1"]["group"]
    assert rows(store)["c1"]["group"] == group

    # Without a rate the whole destination stays pending.
    with pytest.raises(ValueError, match="tasa"):
        store.decide_group("santiago", group, "personal")
    assert all(tx["decision"] is None for tx in rows(store).values())

    assert store.decide_group("santiago", group, "personal", exchange_rate="4000,5") == 3
    decided = rows(store)
    assert decided["u1"]["exchange_rate"] == decided["u2"]["exchange_rate"] == "4000.5"
    assert decided["u1"]["cop_amount_minor"] == 4_000_500
    assert decided["u2"]["cop_amount_minor"] == 10_201_275
    # The peso movement keeps its own amount and gets no rate.
    assert decided["c1"]["exchange_rate"] is None and decided["c1"]["cop_amount_minor"] is None
    assert {tx["decision"] for tx in decided.values()} == {"personal"}


def test_group_rate_is_validated_and_only_for_dollars(make_store):
    store = make_store()
    store.import_message("u1", [purchase("u1", 1000)])
    store.import_message("c1", [purchase("c1", 40000, currency="COP", merchant="TIENDA PESOS")])
    with pytest.raises(ValueError, match="Tasa invalida"):
        store.decide_group("santiago", rows(store)["u1"]["group"], "personal", exchange_rate="cuatro mil")
    with pytest.raises(ValueError, match="solo aplica"):
        store.decide_group("santiago", rows(store)["c1"]["group"], "personal", exchange_rate="4000")
    assert all(tx["decision"] is None for tx in rows(store).values())


def test_a_rule_never_decides_a_dollar_purchase_but_proposes_its_choice(make_store, categories):
    store = make_store()
    category = categories[1]
    store.import_message("u1", [purchase("u1", 1000)])
    store.add_rule("streaming", category, "family")
    assert rows(store)["u1"]["decision"] is None
    assert store.state()["rule_proposals"] == {
        "u1": {"decision": "family", "category": category, "rule": "STREAMING"},
    }

    # A destination rule comes first, as when rules are applied.
    store.import_message("u2", [purchase("u2", 500, merchant="OTRO SERVICIO")])
    store.import_message("c2", [purchase("c2", 9000, currency="COP", merchant="OTRO SERVICIO")])
    group = rows(store)["c2"]["group"]
    store.decide_group("santiago", group, "exclude", pending_only=False, remember=False)
    assert store.state()["rule_proposals"].keys() == {"u1"}
    store.import_message("u3", [purchase("u3", 700, merchant="OTRO SERVICIO")])
    with store.connect() as db:
        db.execute(
            "INSERT INTO group_rules(owner,group_key,decision,category) VALUES ('santiago',?, 'personal', ?)",
            (group, category),
        )
    assert store.state()["rule_proposals"]["u3"] == {"decision": "personal", "category": category, "rule": "destination"}

    # Once it has a rate it is decided, and the proposal goes away.
    store.decide("u1", "family", category, exchange_rate="4000")
    assert "u1" not in store.state()["rule_proposals"]


def test_destination_card_takes_one_rate_and_shows_the_rule_choice(make_store, categories, start_panel, new_page):
    store = make_store()
    category = categories[1]
    store.import_message("u1", [purchase("u1", 1000)])
    store.import_message("u2", [purchase("u2", 2000)])
    store.add_rule("streaming", category, "family")
    page = new_page(viewport={"width": 1280, "height": 1000})
    page.set_default_timeout(3000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{start_panel(store)}/?month=2026-03&currency=COP&tab=decisions")
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-decisions").click()

    card = page.locator("#pending-list .group-card")
    expect(card).to_have_count(1)
    expect(card.locator(":scope > [data-rule-proposal]")).to_have_attribute("data-rule-proposal", "family")
    expect(card.locator(":scope > .field select")).to_have_value(category)
    expect(card.locator(':scope > .decision-actions button.proposed[data-decision="family"]')).to_have_count(1)
    card.get_by_label(re.compile("^Tasa estimada COP por USD para los 2 movimientos")).fill("4100")
    with page.expect_response(lambda response: response.url.endswith("/api/decision-group")) as saved:
        card.get_by_role("button", name=re.compile("^Familiar: 2 movimientos")).click()
    assert saved.value.status == 200
    expect(page.locator("#pending-list .group-card")).to_have_count(0)
    decided = rows(store)
    assert [decided[key]["cop_amount_minor"] for key in ("u1", "u2")] == [4_100_000, 8_200_000]
    assert {decided[key]["category"] for key in ("u1", "u2")} == {category}
    assert {decided[key]["decision"] for key in ("u1", "u2")} == {"family"}
    assert not errors, f"Errores de JavaScript en el panel: {errors}"
